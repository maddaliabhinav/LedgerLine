"""Runs last: after everything the other tests did - races, retries, crashes,
refusals, holds, reversals, tampering-and-restoring - the whole ledger must
still prove out from entry #1. The database backstop may only have fired in
the one test that deliberately sabotages the application's check."""

from __future__ import annotations

from tests.helpers import entries, run_proof


def test_the_whole_ledger_still_proves_out():
    report = run_proof()
    failed = {c["id"]: c["problems"][:5] for c in report["checks"] if not c["ok"] and c["id"] != "backstop"}
    assert not failed, failed
    assert report["totals"]["sum_of_all_balances"] == 0
    assert report["entries"] > 100
    backstop_entries = entries("reason_code = 'INTEGRITY_BACKSTOP'")
    assert all(e["memo"] == "deliberate backstop test" for e in backstop_entries), backstop_entries
