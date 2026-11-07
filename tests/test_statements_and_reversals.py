"""Statements show every debit, credit and refused attempt with the running
balance and who initiated it. Reversals are new entries, never edits."""

from __future__ import annotations

from app import db, hooks
from app.ledger import accounts, engine, statements
from tests.helpers import (RUPEE, admin, all_at_once, assert_books_balance, backstops, balance, entries, key,
                           make_user, open_account)


def statement(account_no: str) -> list[dict]:
    with db.connect() as conn:
        cur = conn.cursor()
        return statements.statement(cur, accounts.find(cur, account_no))


def test_statement_has_debits_credits_refusals_running_balance_and_initiator():
    a, b, thief = make_user(), make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    engine.transfer(a, src, dst, 300 * RUPEE, "rent share", key())
    engine.withdraw(a, src, 100 * RUPEE, "cash", key())
    engine.withdraw(thief, src, 50 * RUPEE, "steal", key())             # refused: not theirs
    engine.transfer(a, src, dst, 5000 * RUPEE, "too much", key())       # refused: no funds
    engine.transfer(b, dst, src, 20 * RUPEE, "change", key())            # a credit
    rows = list(reversed(statement(src)))                                 # oldest first
    assert [r["kind"] for r in rows] == ["DEPOSIT", "TRANSFER", "WITHDRAWAL", "WITHDRAWAL", "TRANSFER", "TRANSFER"]
    assert [r["status"] for r in rows] == ["posted", "posted", "posted", "refused", "refused", "posted"]
    running = 0
    for r in rows:
        if r["status"] == "posted":
            running += r["amount"]["paise"] if r["direction"] == "credit" else -r["amount"]["paise"]
        assert r["balance_after"]["paise"] == running, r                 # refused rows show the unchanged balance
        assert r["initiated_by"] is not None
    assert rows[3]["initiated_by"]["username"] == thief.username and rows[3]["reason_code"] == "NOT_ACCOUNT_OWNER"
    assert rows[4]["reason_code"] == "INSUFFICIENT_FUNDS"
    assert rows[-1]["balance_after"]["paise"] == 620 * RUPEE == balance(src)


def test_reversal_returns_money_with_a_new_entry():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    paid = engine.transfer(a, src, dst, 400 * RUPEE, "wrong payee", key())
    out = engine.reverse(admin(), paid.entry_seq, "customer complaint #42: wrong payee", key())
    assert out.body["status"] == "posted"
    assert balance(src) == 1000 * RUPEE and balance(dst) == 0
    original = entries("seq = %s", (paid.entry_seq,))[0]
    assert original["memo"] == "wrong payee"                              # the original is untouched
    assert entries("seq = %s", (out.entry_seq,))[0]["ref_seq"] == paid.entry_seq
    again = engine.reverse(admin(), paid.entry_seq, "again", key())
    assert again.body["error"]["code"] == "ALREADY_REVERSED"
    assert_books_balance()


def test_concurrent_reversals_reverse_once():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    paid = engine.transfer(a, src, dst, 400 * RUPEE, "x", key())
    before = backstops()
    with hooks.race_window(between_account_locks=0.15):
        results = all_at_once([lambda: engine.reverse(admin(), paid.entry_seq, "duplicate click", key())] * 4)
    statuses = sorted(r.body["status"] for r in results)
    assert statuses == ["posted", "refused", "refused", "refused"]
    assert {r.body["error"]["code"] for r in results if r.body["status"] == "refused"} == {"ALREADY_REVERSED"}
    assert balance(src) == 1000 * RUPEE and balance(dst) == 0
    assert backstops() == before
    assert_books_balance()


def test_a_reversal_cannot_overdraw_the_payee():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    paid = engine.transfer(a, src, dst, 400 * RUPEE, "x", key())
    engine.withdraw(b, dst, 350 * RUPEE, "spent it", key())
    out = engine.reverse(admin(), paid.entry_seq, "chargeback", key())
    assert out.body["error"]["code"] == "INSUFFICIENT_FUNDS"
    assert balance(dst) == 50 * RUPEE
    assert_books_balance()


def test_refusals_cannot_be_reversed():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 10), open_account(b, 0)
    refused = engine.transfer(a, src, dst, 400 * RUPEE, "x", key())
    out = engine.reverse(admin(), refused.entry_seq, "money from nowhere?", key())
    assert out.body["error"]["code"] == "NOT_REVERSIBLE"
    assert balance(src) == 10 * RUPEE and balance(dst) == 0
