"""The record cannot be quietly changed, and if someone with database-owner
rights changes it anyway, the proof (and the offline verifier) catches it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import psycopg
import pytest

from app import db
from app.ledger import accounts, engine, proof
from app.ledger.journal import Line, append_entry
from app.signing import body_hash, entry_body, get_signer
from tests.helpers import (RUPEE, assert_books_balance, backstops, balance, entries, key, make_user, open_account,
                           run_proof)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import verify_bundle  # noqa: E402


def a_transfer() -> tuple[str, str, int]:
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    out = engine.transfer(a, src, dst, 100 * RUPEE, "original memo", key())
    return src, dst, out.entry_seq


# ------------------------------------------------------------------ append-only

def test_journal_rows_cannot_be_updated_deleted_or_truncated():
    _, _, seq = a_transfer()
    with db.connect() as conn:
        for sql in (f"UPDATE journal SET memo = 'hidden' WHERE seq = {seq}",
                    f"UPDATE journal SET amount = 1 WHERE seq = {seq}",
                    f"DELETE FROM journal WHERE seq = {seq}",
                    "TRUNCATE journal CASCADE"):
            with pytest.raises(psycopg.Error) as err:
                conn.execute(sql)
            assert err.value.sqlstate == "LL010", sql


def test_postings_and_duplicate_log_are_append_only():
    _, _, seq = a_transfer()
    with db.connect() as conn:
        for sql in (f"UPDATE postings SET amount = amount * 2 WHERE entry_seq = {seq}",
                    f"DELETE FROM postings WHERE entry_seq = {seq}",
                    "DELETE FROM idempotency_replays",
                    "UPDATE idempotency_keys SET response = '{}'"):
            with pytest.raises(psycopg.Error) as err:
                conn.execute(sql)
            assert err.value.sqlstate in ("LL010", "LL011"), sql


def test_a_balance_cannot_be_set_directly():
    holder = make_user()
    acct = open_account(holder, 10)
    with db.connect() as conn:
        with pytest.raises(psycopg.Error) as err:
            conn.execute("UPDATE accounts SET balance = 999999999 WHERE account_no = %s", (acct,))
        assert err.value.sqlstate == "LL002"
        with pytest.raises(psycopg.Error) as err:
            conn.execute("INSERT INTO accounts (account_no, owner_id, name, kind, balance) "
                         "VALUES ('LL-55555555', %s, 'rich', 'customer', 100000)", (holder.id,))
        assert err.value.sqlstate == "LL001"
        with pytest.raises(psycopg.Error) as err:
            conn.execute("UPDATE accounts SET owner_id = %s WHERE account_no = %s", (make_user().id, acct))
        assert err.value.sqlstate == "LL003"
    assert balance(acct) == 10 * RUPEE


# ------------------------------------------------------------------ the database as backstop

def _locked(cur, *numbers):
    rows = {n: accounts.find(cur, n) for n in numbers}
    locked = accounts.lock(cur, [r["id"] for r in rows.values() if r["kind"] == "customer"])
    return [locked.get(rows[n]["id"], rows[n]) for n in numbers]


def test_database_refuses_an_overdraw_even_if_the_application_check_is_skipped():
    holder = make_user()
    acct = open_account(holder, 1000)
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                cur = conn.cursor()
                src, ext = _locked(cur, acct, "SYS-EXTERNAL")
                append_entry(cur, kind="WITHDRAWAL", status="posted", actor_id=holder.id, account_id=src["id"],
                             counterparty_id=ext["id"], amount=2000 * RUPEE,
                             lines=[Line(src, -2000 * RUPEE), Line(ext, 2000 * RUPEE)])
    assert balance(acct) == 1000 * RUPEE


def test_database_refuses_a_posting_computed_from_a_stale_balance():
    holder = make_user()
    acct = open_account(holder, 1000)
    with db.connect() as conn:
        with pytest.raises(psycopg.Error) as err:
            with conn.transaction():
                cur = conn.cursor()
                src, ext = _locked(cur, acct, "SYS-EXTERNAL")
                stale = {**src, "balance": 5000 * RUPEE}            # what a request without the lock might believe
                append_entry(cur, kind="WITHDRAWAL", status="posted", actor_id=holder.id, account_id=src["id"],
                             counterparty_id=ext["id"], amount=10 * RUPEE,
                             lines=[Line(stale, -10 * RUPEE), Line(ext, 10 * RUPEE)])
        assert err.value.sqlstate == "LL004"
    assert balance(acct) == 1000 * RUPEE


def test_database_refuses_an_unbalanced_entry_at_commit():
    holder = make_user()
    acct = open_account(holder, 0)
    with db.connect() as conn:
        with pytest.raises(psycopg.Error) as err:
            with conn.transaction():
                cur = conn.cursor()
                (dst,) = _locked(cur, acct)
                # A "deposit" that credits the customer but debits nobody: money from nowhere.
                append_entry(cur, kind="DEPOSIT", status="posted", actor_id=holder.id, account_id=dst["id"],
                             amount=500 * RUPEE, lines=[Line(dst, 500 * RUPEE)])
        assert err.value.sqlstate == "LL007"
    assert balance(acct) == 0


def test_postings_cannot_be_attached_to_an_old_entry():
    src, dst, seq = a_transfer()
    with db.connect() as conn:
        with pytest.raises(psycopg.Error) as err:
            with conn.transaction():
                conn.execute("INSERT INTO postings (entry_seq, line_no, account_id, amount, balance_after) "
                             "VALUES (%s, 9, %s, 100, 0)", (seq, accounts.find(conn.cursor(), dst)["id"]))
        assert err.value.sqlstate == "LL008"


def test_the_chain_head_cannot_be_moved_arbitrarily():
    with db.connect() as conn:
        with pytest.raises(psycopg.Error) as err:
            conn.execute("UPDATE ledger_head SET last_seq = last_seq + 5")
        assert err.value.sqlstate == "LL006"


def test_a_hold_cannot_be_decided_twice_at_the_database_level():
    holder = make_user()
    acct = open_account(holder, 1000)
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                cur = conn.cursor()
                src, sus = _locked(cur, acct, "SYS-SUSPENSE")
                hold = append_entry(cur, kind="HOLD", status="posted", actor_id=holder.id, account_id=src["id"],
                                    counterparty_id=src["id"], amount=100,
                                    details={"destination_account_id": src["id"]},
                                    lines=[Line(src, -100), Line(sus, 100)])
                src = {**src, "balance": src["balance"] - 100}
                for _ in range(2):          # two "rejections" would return the money twice
                    append_entry(cur, kind="HOLD_REJECTED", status="posted", actor_id=holder.id,
                                 account_id=src["id"], amount=100, ref_seq=hold["seq"],
                                 lines=[Line(sus, -100), Line(src, 100)])
                    src = {**src, "balance": src["balance"] + 100}
    assert balance(acct) == 1000 * RUPEE


# ------------------------------------------------------------------ tamper evidence

def _with_triggers_disabled(conn, fn):
    """What a database owner could do: switch the guards off. Always restored."""
    conn.execute("ALTER TABLE journal DISABLE TRIGGER journal_append_only")
    conn.execute("ALTER TABLE ledger_head DISABLE TRIGGER ledger_head_guard")
    try:
        return fn()
    finally:
        conn.execute("ALTER TABLE journal ENABLE TRIGGER journal_append_only")
        conn.execute("ALTER TABLE ledger_head ENABLE TRIGGER ledger_head_guard")


def failing(report) -> set[str]:
    return {c["id"] for c in report["checks"] if not c["ok"]}


def test_editing_a_row_is_detected_by_the_proof():
    _, _, seq = a_transfer()
    with db.connect() as conn:
        def tamper_and_check():
            conn.execute("UPDATE journal SET memo = 'nothing to see here' WHERE seq = %s", (seq,))
            try:
                report = run_proof()
                assert "chain" in failing(report)
                assert any(f"#{seq}" in p for c in report["checks"] for p in c["problems"])
            finally:
                conn.execute("UPDATE journal SET memo = 'original memo' WHERE seq = %s", (seq,))
        _with_triggers_disabled(conn, tamper_and_check)
    assert_books_balance()


def test_rewriting_history_and_rehashing_without_the_key_is_detected():
    """The strong attacker: edits a row AND recomputes every later hash so the
    chain looks intact. Without the signing key the signatures cannot follow."""
    _, _, seq = a_transfer()
    with db.connect() as conn:
        originals = conn.execute("SELECT seq, memo, prev_hash, hash FROM journal WHERE seq >= %s ORDER BY seq",
                                 (seq,)).fetchall()
        head = conn.execute("SELECT * FROM ledger_head").fetchone()

        def rewrite_and_check():
            conn.execute("UPDATE journal SET memo = 'rewritten' WHERE seq = %s", (seq,))
            prev = originals[0]["prev_hash"]
            for row in originals:
                full = conn.execute("SELECT * FROM journal WHERE seq = %s", (row["seq"],)).fetchone()
                postings = conn.execute("SELECT * FROM postings WHERE entry_seq = %s", (row["seq"],)).fetchall()
                new_hash = body_hash(entry_body({**full, "prev_hash": prev}, postings))
                conn.execute("UPDATE journal SET prev_hash = %s, hash = %s WHERE seq = %s", (prev, new_hash, row["seq"]))
                prev = new_hash
            conn.execute("UPDATE ledger_head SET last_hash = %s", (prev,))
            try:
                report = run_proof()
                assert "chain" not in failing(report)          # the attacker made the chain consistent...
                assert "signatures" in failing(report)          # ...but could not re-sign it
            finally:
                for row in reversed(originals):
                    conn.execute("UPDATE journal SET memo = %s, prev_hash = %s, hash = %s WHERE seq = %s",
                                 (row["memo"], row["prev_hash"], row["hash"], row["seq"]))
                conn.execute("UPDATE ledger_head SET last_hash = %s", (head["last_hash"],))
        _with_triggers_disabled(conn, rewrite_and_check)
    assert_books_balance()


def test_offline_verifier_agrees_with_the_server_and_catches_tampering(tmp_path):
    a_transfer()
    with db.connect() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        bundle = proof.export_bundle(cur)
    public_key = get_signer().public_key_b64
    assert verify_bundle.verify(bundle, public_key)["ok"]

    tampered = json.loads(json.dumps(bundle))
    victim = next(e for e in tampered["entries"] if e["body"]["kind"] == "TRANSFER" and e["body"]["status"] == "posted")
    victim["body"]["postings"][1]["amount"] += 100          # pay the payee one rupee more
    result = verify_bundle.verify(tampered, public_key)
    assert not result["ok"]
    assert any("content does not match its hash" in p for p in result["problems"])

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import base64
    other_key = Ed25519PrivateKey.generate()
    forged_pub = base64.b64encode(other_key.public_key().public_bytes_raw()).decode()
    assert not verify_bundle.verify(bundle, forged_pub)["ok"]   # a different key does not vouch for it


def test_proof_passes_on_an_honest_ledger():
    a_transfer()
    report = run_proof()
    assert {c["id"] for c in report["checks"] if not c["ok"]} <= {"backstop"}
    assert report["totals"]["sum_of_all_balances"] == 0


def test_engine_turns_a_database_refusal_into_a_recorded_refusal(monkeypatch):
    """Sabotage the application's balance check (as a buggy release would) and
    show the last line of defence: PostgreSQL refuses the overdraw, the engine
    rolls back to its savepoint, records INTEGRITY_BACKSTOP, and nothing moves."""
    holder = make_user()
    acct = open_account(holder, 100)
    before = backstops()
    monkeypatch.setattr(accounts, "available", lambda account: 10**15)      # the "bug"
    out = engine.withdraw(holder, acct, 500 * RUPEE, "deliberate backstop test", key())
    assert out.body["status"] == "refused" and out.body["error"]["code"] == "INTEGRITY_BACKSTOP"
    assert balance(acct) == 100 * RUPEE
    refusal = entries("seq = %s", (out.entry_seq,))[0]
    assert refusal["details"]["sqlstate"] in ("23514", "LL004")
    assert backstops() == before + 1
    assert_books_balance()
