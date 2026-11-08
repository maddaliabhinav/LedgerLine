#!/usr/bin/env python3
"""Do the tests actually test anything? Remove one lock or one check at a time
and prove the test suite FAILS.

The judges said: "We will take one lock or one check out of your code and run
your tests; if they still pass, they were not testing anything." This script
does exactly that, for every protection we rely on, and reports which test
caught each removal. A mutation that SURVIVES (tests still pass) is a gap.

    python tools/mutation_check.py              # all mutations (takes several minutes)
    python tools/mutation_check.py M01 M13      # just these
    python tools/mutation_check.py --report docs/MUTATION_REPORT.md

Needs the same test database as the test suite (TEST_DATABASE_URL).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Mutation:
    id: str
    title: str
    file: str
    old: str
    new: str
    occurrence: int = 1        # which occurrence of `old` to replace (1-based)


ENGINE = "app/ledger/engine.py"
SCHEMA = "app/schema.sql"
ADMIN_OPS = "app/ledger/admin_ops.py"
ACCESS_SQL = "app/migrations/002_access_requests.sql"
BALANCE_CHECK = 'if amount > accounts.available(source):                    # THE balance check'
OWNER_CHECK = 'if source["owner_id"] != actor.id:'
DAILY_CHECK = 'if _daily_outgoing(cur, source["id"]) + amount > source["daily_limit"]:'

MUTATIONS = [
    # --- locks -------------------------------------------------------------
    Mutation("M01", "Remove the row lock on accounts (SELECT ... FOR UPDATE)", "app/ledger/accounts.py",
             "AND kind = 'customer' FOR UPDATE\"", "AND kind = 'customer'\""),
    Mutation("M02", "Lock accounts in arrival order instead of ascending id", "app/ledger/accounts.py",
             "for account_id in sorted(set(account_ids)):", "for account_id in list(dict.fromkeys(account_ids)):"),
    Mutation("M03", "Remove the chain-head lock", "app/ledger/journal.py",
             "FROM ledger_head WHERE id = 1 FOR UPDATE\"", "FROM ledger_head WHERE id = 1\""),
    # --- application checks --------------------------------------------------
    Mutation("M04", "Remove the balance check in withdraw", ENGINE, BALANCE_CHECK, "if False:", 1),
    Mutation("M05", "Remove the balance check in transfer", ENGINE, BALANCE_CHECK, "if False:", 2),
    Mutation("M06", "Remove the owner check in withdraw", ENGINE, OWNER_CHECK, "if False:", 1),
    Mutation("M07", "Remove the owner check in transfer", ENGINE, OWNER_CHECK, "if False:", 2),
    Mutation("M08", "Remove the admin-only check on deposits", ENGINE,
             'if actor.role != "admin":                                  # decided by the server, every time',
             "if False:"),
    Mutation("M09", "Remove the role check on transfers", ENGINE,
             'if actor.role != "holder":\n            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")\n'
             '        if source["owner_id"] != actor.id:\n            return _refuse(ctx, attempt, "NOT_ACCOUNT_OWNER")\n'
             '        if destination is None:',
             'if False:\n            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")\n'
             '        if source["owner_id"] != actor.id:\n            return _refuse(ctx, attempt, "NOT_ACCOUNT_OWNER")\n'
             '        if destination is None:'),
    Mutation("M10", "Remove the daily-limit check in transfer", ENGINE, DAILY_CHECK, "if False:", 2),
    Mutation("M11", "Remove the frozen-destination check", ENGINE,
             'if destination["status"] != "active":\n            return _refuse(ctx, attempt, "DESTINATION_NOT_ACTIVE")',
             'if False:\n            return _refuse(ctx, attempt, "DESTINATION_NOT_ACTIVE")'),
    Mutation("M12", "Remove the 'already decided' check on held payments", ENGINE,
             'if cur.fetchone() is not None:\n            return _refuse(ctx, attempt, "HOLD_ALREADY_DECIDED")',
             'if False:\n            return _refuse(ctx, attempt, "HOLD_ALREADY_DECIDED")'),
    Mutation("M13", "Remove the 'already reversed' check", ENGINE,
             'if cur.fetchone() is not None:\n            return _refuse(ctx, attempt, "ALREADY_REVERSED")',
             'if False:\n            return _refuse(ctx, attempt, "ALREADY_REVERSED")'),
    Mutation("M14", "Pay risky transfers without human review", ENGINE, "if assessment.hold:", "if False:", 2),
    # --- idempotency -----------------------------------------------------------
    Mutation("M15", "Process duplicates as new requests (skip the claim result)", ENGINE,
             "if claimed:", "if True:"),
    Mutation("M16", "Ignore the request fingerprint on a reused key", "app/ledger/idempotency.py",
             'if stored["fingerprint"] != fp or stored["endpoint"] != endpoint:', "if False:"),
    # --- database guarantees ----------------------------------------------------
    Mutation("M17", "Drop the balance_floor CHECK constraint", SCHEMA,
             "    CONSTRAINT balance_floor CHECK (allow_negative OR balance >= -overdraft_limit),\n", ""),
    Mutation("M18", "Drop the stale-balance tripwire (LL004)", SCHEMA,
             "IF actual <> NEW.balance_after THEN", "IF FALSE THEN"),
    Mutation("M19", "Allow direct balance updates (LL002)", SCHEMA,
             "IF NEW.balance IS DISTINCT FROM OLD.balance AND pg_trigger_depth() < 2 THEN", "IF FALSE THEN"),
    Mutation("M20", "Drop the double-entry check at commit (LL007)", SCHEMA,
             "IF n < 2 OR credits <> NEW.amount OR total <> 0 THEN", "IF FALSE THEN"),
    Mutation("M21", "Drop the append-only trigger on the journal", SCHEMA,
             "CREATE TRIGGER journal_append_only BEFORE UPDATE OR DELETE ON journal\n"
             "    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();\n", ""),
    Mutation("M22", "Drop the unique (user, key) index on the journal", SCHEMA,
             "CREATE UNIQUE INDEX journal_idempotency\n"
             "    ON journal (actor_id, idempotency_key) WHERE idempotency_key IS NOT NULL;\n", ""),
    Mutation("M23", "Drop the one-decision-per-hold index", SCHEMA,
             "CREATE UNIQUE INDEX journal_one_decision_per_hold\n"
             "    ON journal (ref_seq) WHERE kind IN ('HOLD_APPROVED', 'HOLD_REJECTED') AND status = 'posted';\n", ""),
    Mutation("M24", "Allow postings on an old entry (LL008)", SCHEMA,
             "IF j_txid IS NULL OR j_txid <> txid_current() THEN", "IF FALSE THEN"),
    Mutation("M25", "Allow the chain head to jump (LL006)", SCHEMA,
             "IF NEW.last_seq <> OLD.last_seq + 1 OR NOT EXISTS (", "IF FALSE AND NOT EXISTS ("),
    Mutation("M26", "Make idempotency records editable", SCHEMA,
             "IF OLD.completed_at IS NOT NULL THEN", "IF FALSE THEN"),
    # --- auth --------------------------------------------------------------------
    Mutation("M27", "Keep accepting tokens of deactivated users", "app/api/deps.py",
             'if user is None or not user["is_active"] or user["token_version"] != claims["tv"]:',
             "if user is None:"),
    Mutation("M28", "Trust the role written inside the token", "app/api/deps.py",
             'return CurrentUser(user["id"], user["username"], user["display_name"], user["role"])',
             'return CurrentUser(user["id"], user["username"], user["display_name"], claims.get("role", user["role"]))'),
    Mutation("M29", "Let holders read any statement", "app/api/routes_accounts.py",
             'if user.role == "holder" and acct["owner_id"] != user.id:', "if False:"),
    Mutation("M30", "Accept negative integer amounts", "app/money.py", "if value < 0:", "if False:"),
    Mutation("M31", "Stop throttling password guesses", "app/security.py",
             'raise TooManyRequests("too many failed sign-in attempts; wait a few minutes",', 'print("throttle off",'),
    # --- proof and AI guardrails ------------------------------------------------------
    Mutation("M32", "Proof stops recomputing hashes", "app/ledger/proof.py",
             'if recomputed != row["hash"]:\n            checks["chain"].fail(f"#{seq}: content does not match its hash',
             'if False:\n            checks["chain"].fail(f"#{seq}: content does not match its hash'),
    Mutation("M33", "Proof stops checking signatures", "app/ledger/proof.py",
             'if row["key_id"] != signer.key_id or not verify_signature(signer.public_bytes, row["hash"], row["signature"]):',
             "if False:"),
    Mutation("M34", "Show unchecked AI text to reviewers (no grounding)", "app/sentinel/narrator.py",
             "bad = grounding_check(text, facts)", "bad = []"),
    Mutation("M35", "Send the user's memo to the LLM", "app/sentinel/narrator.py",
             '"amount": hold["amount"],', '"amount": hold["amount"], "memo": hold["memo"],'),
    # --- public sign-up --------------------------------------------------------------
    Mutation("M36", "Public sign-up creates administrators", "app/ledger/admin_ops.py",
             "VALUES (%s, %s, 'holder', %s)", "VALUES (%s, %s, 'admin', %s)"),
    Mutation("M37", "Remove the sign-up rate limit", "app/api/routes_auth.py",
             "signup_throttle.attempt(_client_ip(request))", "pass"),
    # --- auditor access: requested at sign-up, granted only by an administrator ---------
    Mutation("M38", "Let any signed-in user decide auditor requests", "app/api/routes_admin.py",
             'deciders = require_roles("admin")', 'deciders = require_roles("admin", "auditor", "holder")'),
    Mutation("M39", "Decide an auditor request twice (skip the status check)", ADMIN_OPS,
             'if req["status"] != "pending":', "if False:"),
    Mutation("M40", "Decide auditor requests without locking the request row", ADMIN_OPS,
             'FROM access_requests WHERE id = %s FOR UPDATE"', 'FROM access_requests WHERE id = %s"'),
    Mutation("M41", "A waiting request no longer reserves its username", ADMIN_OPS,
             'if waiting is not None and waiting["id"] != request_id:', "if False:"),
    Mutation("M42", "Database accepts requests for admin access", ACCESS_SQL,
             "role          TEXT NOT NULL CHECK (role = 'auditor'),", "role          TEXT NOT NULL,"),
    Mutation("M43", "Database lets a non-admin's entry decide a request (LL012)", ACCESS_SQL,
             "AND decider.role = 'admin') THEN", ") THEN"),
    Mutation("M44", "Database lets a decided request be decided again (LL012)", ACCESS_SQL,
             "IF OLD.status <> 'pending' THEN", "IF FALSE THEN"),
    Mutation("M45", "Make user roles editable (LL013)", ACCESS_SQL,
             "IF NEW.role <> OLD.role THEN", "IF FALSE THEN"),
    Mutation("M46", "Drop the users-on-the-record check at commit (LL014)", ACCESS_SQL,
             "CREATE CONSTRAINT TRIGGER users_created_on_record AFTER INSERT ON users\n"
             "    DEFERRABLE INITIALLY DEFERRED\n"
             "    FOR EACH ROW EXECUTE FUNCTION users_created_on_record();\n", ""),
    Mutation("M47", "Proof stops comparing roles with the record", "app/ledger/proof.py",
             'if entry["details"].get("role") != user["role"]:', "if False:"),
]


def apply(workdir: Path, m: Mutation) -> None:
    path = workdir / m.file
    text = path.read_text(encoding="utf-8")
    starts = [match.start() for match in re.finditer(re.escape(m.old), text)]
    if len(starts) < m.occurrence:
        raise SystemExit(f"{m.id}: pattern not found ({len(starts)} occurrence(s)) in {m.file} - update the mutation")
    at = starts[m.occurrence - 1]
    path.write_text(text[:at] + m.new + text[at + len(m.old):], encoding="utf-8")


def run_suite(workdir: Path, timeout: int) -> tuple[bool, str, float]:
    started = time.time()
    try:
        proc = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:randomly"], cwd=workdir,
                              capture_output=True, text=True, timeout=timeout, env=os.environ.copy())
    except subprocess.TimeoutExpired:
        return False, "TIMEOUT (counts as caught: the suite did not pass)", time.time() - started
    elapsed = time.time() - started
    if proc.returncode == 0:
        return True, "", elapsed
    failed = re.findall(r"FAILED (\S+)", proc.stdout) or re.findall(r"ERROR (\S+)", proc.stdout)
    return False, (failed[0] if failed else proc.stdout.strip().splitlines()[-1]), elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ids", nargs="*", help="mutation ids to run (default: all)")
    parser.add_argument("--report", help="write a Markdown report to this path")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()
    chosen = [m for m in MUTATIONS if not args.ids or m.id in args.ids]

    print("baseline: running the unmodified suite ...", flush=True)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "repo"
        shutil.copytree(ROOT, base, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "*.pyc"))
        survived, _, secs = run_suite(base, args.timeout)
        if not survived:
            print("the unmodified suite fails - fix that first")
            return 2
        print(f"baseline passes ({secs:.0f}s)\n")

    rows = []
    for m in chosen:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "repo"
            shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "venv", "*.pyc"))
            apply(work, m)
            survived, caught_by, secs = run_suite(work, args.timeout)
        status = "SURVIVED" if survived else "caught"
        rows.append((m, status, caught_by, secs))
        print(f"{m.id} {status:8s} {m.title}\n        {caught_by or '(no test failed!)'}  [{secs:.0f}s]", flush=True)

    caught = sum(1 for _, s, _, _ in rows if s == "caught")
    print(f"\n{caught}/{len(rows)} mutations caught by the test suite")
    if args.report:
        lines = ["# Mutation report", "",
                 "Generated by `python tools/mutation_check.py --report docs/MUTATION_REPORT.md`.",
                 "Each row removes ONE lock or check from a copy of the code and runs the full test suite.",
                 "\"caught\" means at least one test failed; the first failing test is shown.", "",
                 f"**{caught} of {len(rows)} mutations caught.**", "",
                 "| id | what was removed | result | first test that failed |",
                 "|---|---|---|---|"]
        for m, status, caught_by, _ in rows:
            lines.append(f"| {m.id} | {m.title} (`{m.file}`) | {status} | `{caught_by}` |")
        Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"report written to {args.report}")
    return 0 if caught == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())
