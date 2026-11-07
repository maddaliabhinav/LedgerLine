"""Exactly once: a request that arrives twice moves money once and answers the same."""

from __future__ import annotations

import dataclasses
import threading
import time

import psycopg
import pytest

from app import db, hooks
from app.config import get_settings
from app.errors import IdempotencyKeyReused, RequestInProgress
from app.ledger import accounts, engine
from app.ledger.journal import Line, append_entry
from tests.helpers import (RUPEE, admin, all_at_once, assert_books_balance, backstops, balance, entries, key,
                           make_user, open_account)


def replays(k: str) -> list[dict]:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM idempotency_replays WHERE idem_key = %s ORDER BY id", (k,)).fetchall()


def test_same_key_twice_moves_money_once_and_returns_the_same_bytes():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    first = engine.transfer(a, src, dst, 300 * RUPEE, "rent", k)
    second = engine.transfer(a, src, dst, 300 * RUPEE, "rent", k)
    assert first.body["status"] == "posted"
    assert not first.replayed and second.replayed
    assert second.render() == first.render()                      # byte-for-byte the same answer
    assert second.headers["Idempotent-Replayed"] == "true"
    assert balance(src) == 700 * RUPEE and balance(dst) == 300 * RUPEE
    assert len(entries("idempotency_key = %s", (k,))) == 1
    assert [r["outcome"] for r in replays(k)] == ["replayed"]      # the duplicate is visible in the data
    assert_books_balance()


def test_twenty_copies_at_the_same_instant_move_money_once():
    """A double-click / retry storm: same key, fired together. The first claims
    the key and holds it (uncommitted) for 0.3 s; the other 19 wait on the
    key's primary key and then get the first one's answer."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    before = backstops()
    with hooks.race_window(after_idempotency_claim=0.3):
        results = all_at_once([lambda: engine.transfer(a, src, dst, 100 * RUPEE, "storm", k)] * 20)
    assert not [r for r in results if isinstance(r, Exception)], results
    assert sum(1 for r in results if not r.replayed) == 1
    assert len({r.render() for r in results}) == 1
    assert balance(src) == 900 * RUPEE and balance(dst) == 100 * RUPEE
    assert len(entries("idempotency_key = %s", (k,))) == 1
    assert len(replays(k)) == 19
    assert backstops() == before
    assert_books_balance()


def test_retry_arriving_while_the_first_is_in_flight_gets_the_first_answer():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    out = {}

    def first():
        out["first"] = engine.transfer(a, src, dst, 250 * RUPEE, "slow", k)

    with hooks.race_window(after_idempotency_claim=1.0):
        thread = threading.Thread(target=first)
        thread.start()
        time.sleep(0.3)                           # the first now holds the key, uncommitted
        started = time.monotonic()
        second = engine.transfer(a, src, dst, 250 * RUPEE, "slow", k)
        waited = time.monotonic() - started
        thread.join()
    assert waited > 0.4, "the retry should have waited for the first request to finish"
    assert second.replayed and second.render() == out["first"].render()
    assert balance(dst) == 250 * RUPEE
    assert_books_balance()


def test_retry_that_would_wait_too_long_is_told_to_come_back_and_moves_nothing(monkeypatch):
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    fast = dataclasses.replace(get_settings(), lock_timeout_ms=200)
    monkeypatch.setattr(engine, "get_settings", lambda: fast)
    out = {}

    def first():
        out["first"] = engine.transfer(a, src, dst, 100 * RUPEE, "slow", k)

    with hooks.race_window(after_idempotency_claim=1.2):
        thread = threading.Thread(target=first)
        thread.start()
        time.sleep(0.3)
        with pytest.raises(RequestInProgress):
            engine.transfer(a, src, dst, 100 * RUPEE, "slow", k)
        thread.join()
    later = engine.transfer(a, src, dst, 100 * RUPEE, "slow", k)
    assert later.replayed and later.render() == out["first"].render()
    assert balance(dst) == 100 * RUPEE
    assert [r["outcome"] for r in replays(k)] == ["in_flight_timeout", "replayed"]
    assert_books_balance()


def test_same_key_with_a_different_request_is_refused():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    engine.transfer(a, src, dst, 100 * RUPEE, "first", k)
    with pytest.raises(IdempotencyKeyReused):
        engine.transfer(a, src, dst, 900 * RUPEE, "first", k)        # same key, different amount
    with pytest.raises(IdempotencyKeyReused):
        engine.withdraw(a, src, 100 * RUPEE, "first", k)             # same key, different endpoint
    assert balance(src) == 900 * RUPEE and balance(dst) == 100 * RUPEE
    assert [r["outcome"] for r in replays(k)] == ["key_reused_with_different_request"] * 2
    assert_books_balance()


def test_keys_are_scoped_per_user():
    a, b, c = make_user(), make_user(), make_user()
    acct_a, acct_b, acct_c = open_account(a, 500), open_account(b, 500), open_account(c, 0)
    k = key()
    ra = engine.transfer(a, acct_a, acct_c, 100 * RUPEE, "same key", k)
    rb = engine.transfer(b, acct_b, acct_c, 200 * RUPEE, "same key", k)
    assert not rb.replayed and rb.render() != ra.render()           # b never sees a's answer
    assert balance(acct_c) == 300 * RUPEE
    assert_books_balance()


def test_a_refusal_is_replayed_not_re_evaluated():
    """A key is bound to its first outcome. Topping up and retrying the same key
    does not suddenly pay: that would make the outcome depend on timing."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 100), open_account(b, 0)
    k = key()
    first = engine.transfer(a, src, dst, 500 * RUPEE, "too much", k)
    assert first.body["error"]["code"] == "INSUFFICIENT_FUNDS"
    engine.deposit(admin(), src, 1000 * RUPEE, "top up", key())
    again = engine.transfer(a, src, dst, 500 * RUPEE, "too much", k)
    assert again.replayed and again.render() == first.render()
    assert balance(dst) == 0
    assert_books_balance()


def test_database_refuses_two_journal_entries_with_one_key():
    """Backstop below the idempotency table: one (user, key) -> one journal entry."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                cur = conn.cursor()
                locked = accounts.lock(cur, [accounts.find(cur, src)["id"], accounts.find(cur, dst)["id"]])
                s, d = (locked[accounts.find(cur, n)["id"]] for n in (src, dst))
                for _ in range(2):
                    append_entry(cur, kind="TRANSFER", status="posted", actor_id=a.id, account_id=s["id"],
                                 counterparty_id=d["id"], amount=10 * RUPEE, idempotency_key=k,
                                 lines=[Line(s, -10 * RUPEE), Line(d, 10 * RUPEE)])
                    s = {**s, "balance": s["balance"] - 10 * RUPEE}
                    d = {**d, "balance": d["balance"] + 10 * RUPEE}
    assert balance(src) == 1000 * RUPEE
