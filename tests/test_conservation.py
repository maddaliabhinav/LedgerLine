"""Property: whatever mix of operations runs, concurrently, money is conserved.

A seeded random workload of deposits, withdrawals, transfers (some large
enough to be held), approvals, rejections and reversals runs on 12 threads.
Afterwards the full proof must pass, and the change in money held by these
accounts must equal external money in minus external money out, minus what is
still parked under review.
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor

from app import hooks
from app.ledger import engine
from tests.helpers import RUPEE, admin, assert_books_balance, backstops, balance, key, make_user, open_account


def test_random_concurrent_workload_conserves_money():
    rng = random.Random(20261106)
    holders = [make_user() for _ in range(4)]
    owned = [(h, open_account(h, rng.randint(2_000, 1_50_000))) for h in holders for _ in range(2)]
    numbers = [n for _, n in owned]
    start_total = sum(balance(n) for n in numbers)
    before = backstops()

    def random_op(i: int):
        r = random.Random(i)
        holder, acct = r.choice(owned)
        roll = r.random()
        amount = r.choice([1, 50, 999, 5_000, 30_000, 1_20_000]) * RUPEE
        if roll < 0.55:
            dst = r.choice([n for n in numbers if n != acct])
            return ("transfer", amount, engine.transfer(holder, acct, dst, amount, f"op{i}", key()))
        if roll < 0.75:
            return ("withdraw", amount, engine.withdraw(holder, acct, amount, f"op{i}", key()))
        return ("deposit", amount, engine.deposit(admin(), acct, amount, f"op{i}", key()))

    with hooks.race_window(after_balance_check=0.003):
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(random_op, range(240)))

    # Decide some of the held payments, concurrently with a few more operations.
    held = [out.entry_seq for _, _, out in results if out.body["status"] == "held"]
    decisions = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, seq in enumerate(held):
            choice = "approve" if i % 2 == 0 else "reject"
            decisions.append(pool.submit(engine.decide_hold, admin(), seq, choice, "workload", key()))
        extra = [pool.submit(random_op, 1000 + i) for i in range(30)]
    results += [f.result() for f in extra]
    decided = {d.result().body["entry"]["ref_seq"]: d.result() for d in decisions}

    deposited = sum(a for kind, a, out in results if kind == "deposit" and out.body["status"] == "posted")
    withdrawn = sum(a for kind, a, out in results if kind == "withdraw" and out.body["status"] == "posted")
    # Held withdrawals that were approved left the system; held payments not yet
    # approved still sit in suspense; approved held transfers stayed inside our accounts.
    still_held = 0
    left_via_approval = 0
    for kind, amount, out in results:
        if out.body["status"] != "held":
            continue
        decision = decided.get(out.entry_seq)
        if decision is None:
            still_held += amount
        elif decision.body["entry"]["kind"] == "HOLD_APPROVED" and kind == "withdraw":
            left_via_approval += amount

    end_total = sum(balance(n) for n in numbers)
    assert end_total == start_total + deposited - withdrawn - still_held - left_via_approval
    assert all(balance(n) >= 0 for n in numbers)
    assert backstops() == before
    assert_books_balance()
