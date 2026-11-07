"""Races: the same money asked for by several requests at the same instant.

Every test here really runs requests in parallel threads, released together by
a barrier. `hooks.race_window(...)` widens the gap between "check" and "write"
so that, if a lock were missing, the requests WOULD interleave badly; with the
locks in place they queue up instead. Each test also asserts that the
database backstop never fired: if it did, a lock or check in the application
failed and only PostgreSQL saved the money - which we count as a failure.
"""

from __future__ import annotations

import threading
import time

import psycopg
import pytest

from app import db, hooks
from app.ledger import admin_ops, engine
from tests.helpers import (RUPEE, admin, all_at_once, assert_books_balance, backstops, balance, entries, key,
                           make_user, open_account)


def codes(results) -> list[str]:
    out = []
    for r in results:
        if isinstance(r, Exception):
            out.append(type(r).__name__)
        elif r.body["status"] == "refused":
            out.append(r.body["error"]["code"])
        else:
            out.append(r.body["status"])
    return out


def test_two_withdrawals_at_the_same_instant_cannot_overdraw():
    """The judges' first attack: Rs 1,000 in the account, two Rs 600 withdrawals at once."""
    holder = make_user()
    acct = open_account(holder, 1000)
    before = backstops()
    with hooks.race_window(after_balance_check=0.3):
        results = all_at_once([lambda: engine.withdraw(holder, acct, 600 * RUPEE, "race", key())] * 2)
    assert sorted(codes(results)) == ["INSUFFICIENT_FUNDS", "posted"]
    assert balance(acct) == 400 * RUPEE
    assert backstops() == before
    assert_books_balance()


def test_twenty_withdrawals_at_once_pay_exactly_what_fits():
    holder = make_user()
    acct = open_account(holder, 1000)
    before = backstops()
    with hooks.race_window(after_balance_check=0.02):
        results = all_at_once([lambda: engine.withdraw(holder, acct, 100 * RUPEE, "burst", key())] * 20)
    c = codes(results)
    assert c.count("posted") == 10 and c.count("INSUFFICIENT_FUNDS") == 10, c
    assert balance(acct) == 0
    assert backstops() == before
    assert_books_balance()


def test_concurrent_transfers_from_one_account_cannot_overdraw():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    before = backstops()
    with hooks.race_window(after_balance_check=0.15):
        results = all_at_once([lambda: engine.transfer(a, src, dst, 300 * RUPEE, "race", key())] * 5)
    c = codes(results)
    assert c.count("posted") == 3 and c.count("INSUFFICIENT_FUNDS") == 2, c
    assert balance(src) == 100 * RUPEE and balance(dst) == 900 * RUPEE
    assert backstops() == before
    assert_books_balance()


def test_withdrawal_and_transfer_racing_for_the_same_money():
    """Different routes to the same rupees: only one may win."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    before = backstops()
    with hooks.race_window(after_balance_check=0.3):
        results = all_at_once([
            lambda: engine.withdraw(a, src, 700 * RUPEE, "cash", key()),
            lambda: engine.transfer(a, src, dst, 700 * RUPEE, "pay", key()),
        ])
    assert sorted(codes(results)) == ["INSUFFICIENT_FUNDS", "posted"]
    assert balance(src) == 300 * RUPEE
    assert balance(src) + balance(dst) in (300 * RUPEE, 1000 * RUPEE)
    assert backstops() == before
    assert_books_balance()


def test_opposite_transfers_do_not_deadlock():
    """A->B and B->A at the same time. Locks are taken in ascending account id,
    so they queue instead of deadlocking."""
    a, b = make_user(), make_user()
    acct_a, acct_b = open_account(a, 1000), open_account(b, 1000)
    with hooks.race_window(between_account_locks=0.2):
        results = all_at_once([
            lambda: engine.transfer(a, acct_a, acct_b, 10 * RUPEE, "a->b", key()),
            lambda: engine.transfer(b, acct_b, acct_a, 20 * RUPEE, "b->a", key()),
        ] * 3)
    assert codes(results) == ["posted"] * 6, codes(results)
    assert balance(acct_a) == 1030 * RUPEE and balance(acct_b) == 970 * RUPEE
    assert_books_balance()


def test_unrelated_transfers_queue_on_the_chain_head():
    """Two transfers on four different accounts touch no common row - except the
    chain head, which gives every entry a unique, gapless sequence number."""
    users = [make_user() for _ in range(4)]
    accts = [open_account(u, 500) for u in users]
    before = backstops()
    head_before = entries("TRUE")[-1]["seq"]
    with hooks.race_window(after_chain_head=0.3):
        results = all_at_once([
            lambda: engine.transfer(users[0], accts[0], accts[1], 50 * RUPEE, "p1", key()),
            lambda: engine.transfer(users[2], accts[2], accts[3], 70 * RUPEE, "p2", key()),
        ])
    assert codes(results) == ["posted", "posted"]
    seqs = sorted(r.entry_seq for r in results)
    assert seqs == [head_before + 1, head_before + 2]
    assert backstops() == before
    assert_books_balance()


def test_overdraft_floor_holds_under_a_race():
    holder = make_user()
    acct = open_account(holder, 1000)
    admin_ops.set_overdraft(admin().id, acct, 500 * RUPEE, "approved credit line")
    before = backstops()
    with hooks.race_window(after_balance_check=0.15):
        results = all_at_once([lambda: engine.withdraw(holder, acct, 600 * RUPEE, "od", key())] * 3)
    c = codes(results)
    assert c.count("posted") == 2 and c.count("INSUFFICIENT_FUNDS") == 1, c
    assert balance(acct) == -200 * RUPEE          # inside the authorised floor of -500
    assert backstops() == before
    assert_books_balance()


def test_daily_limit_holds_under_a_race():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 50_000), open_account(b, 0)
    with db.connect() as conn:
        conn.execute("UPDATE accounts SET daily_limit = %s WHERE account_no = %s", (10_000 * RUPEE, src))
    before = backstops()
    with hooks.race_window(after_balance_check=0.1):
        results = all_at_once([lambda: engine.transfer(a, src, dst, 3_000 * RUPEE, "limit", key())] * 5)
    c = codes(results)
    assert c.count("posted") == 3 and c.count("DAILY_LIMIT_EXCEEDED") == 2, c
    assert balance(dst) == 9_000 * RUPEE
    assert backstops() == before
    assert_books_balance()


def test_freeze_racing_transfers_is_all_or_nothing():
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    with hooks.race_window(after_balance_check=0.1):
        results = all_at_once(
            [lambda: engine.transfer(a, src, dst, 100 * RUPEE, "x", key())] * 4
            + [lambda: admin_ops.set_status(admin().id, src, "frozen", "suspected compromise")])
    transfers = codes(results[:4])
    assert set(transfers) <= {"posted", "ACCOUNT_NOT_ACTIVE"}
    paid = transfers.count("posted")
    assert balance(src) == (1000 - 100 * paid) * RUPEE and balance(dst) == 100 * paid * RUPEE
    refused_after_freeze = engine.transfer(a, src, dst, 1 * RUPEE, "after", key())
    assert refused_after_freeze.body["error"]["code"] == "ACCOUNT_NOT_ACTIVE"
    assert_books_balance()


def test_failure_between_debit_and_credit_leaves_no_trace():
    """'A transfer debits one account and the credit is lost on the way.'
    We crash the request after the debit posting is written: everything rolls back."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    with hooks.faults("between_postings"):
        with pytest.raises(hooks.InjectedFault):
            engine.transfer(a, src, dst, 250 * RUPEE, "crash", k)
    assert balance(src) == 1000 * RUPEE and balance(dst) == 0
    assert entries("idempotency_key = %s", (k,)) == []
    with db.connect() as conn:            # the key was not consumed: a retry is a fresh attempt
        assert conn.execute("SELECT count(*) AS n FROM idempotency_keys WHERE idem_key = %s", (k,)).fetchone()["n"] == 0
    retry = engine.transfer(a, src, dst, 250 * RUPEE, "crash", k)
    assert retry.body["status"] == "posted"
    again = engine.transfer(a, src, dst, 250 * RUPEE, "crash", k)
    assert again.replayed and again.render() == retry.render()
    assert balance(src) == 750 * RUPEE and balance(dst) == 250 * RUPEE
    assert_books_balance()


def test_connection_killed_mid_transfer_moves_nothing():
    """Kill the database session in the middle of a transfer (process crash,
    network cut). PostgreSQL rolls the transaction back: no half-transfer."""
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    k = key()
    outcome = {}

    def victim():
        try:
            outcome["result"] = engine.transfer(a, src, dst, 400 * RUPEE, "killed", k)
        except Exception as exc:          # expected: the connection dies under it
            outcome["error"] = exc

    with hooks.watch_backend("between_postings") as pids, hooks.race_window(between_postings=1.0):
        thread = threading.Thread(target=victim)
        thread.start()
        deadline = time.time() + 5
        while not pids and time.time() < deadline:
            time.sleep(0.02)
        assert pids, "transfer never reached the point between debit and credit"
        with db.connect() as conn:
            conn.execute("SELECT pg_terminate_backend(%s)", (pids[0],))
        thread.join(10)
    assert "error" in outcome and isinstance(outcome["error"], psycopg.Error)
    assert balance(src) == 1000 * RUPEE and balance(dst) == 0
    assert entries("idempotency_key = %s", (k,)) == []
    retry = engine.transfer(a, src, dst, 400 * RUPEE, "killed", k)
    assert retry.body["status"] == "posted"
    assert balance(src) == 600 * RUPEE
    assert_books_balance()
