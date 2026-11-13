"""Command line: python -m app.cli <command>

  migrate            create the schema and the GENESIS entry, or upgrade an existing one (idempotent)
  seed [--if-empty]  demo users, accounts and history (through the real engine), and one set
                     of personal details waiting for an administrator
  verify             run the full proof and print the result (exit 1 if anything fails)
  export FILE        write an evidence bundle for tools/verify_bundle.py
  keygen             print a fresh random LEDGER_SIGNING_KEY value
  reset --yes        DROP the whole schema (refused when APP_ENV=production)
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from datetime import date
from pathlib import Path

from . import db
from .config import get_settings
from .ledger import admin_ops, details, engine, proof
from .ledger.journal import append_entry
from .signing import get_signer

SCHEMA_FILE = Path(__file__).with_name("schema.sql")             # version 1
MIGRATIONS_DIR = Path(__file__).with_name("migrations")          # 002_*.sql, 003_*.sql, ... on top of it
MIGRATION_LOCK = 727_001


def _migrations() -> list[tuple[int, Path]]:
    return sorted((int(p.name[:3]), p) for p in MIGRATIONS_DIR.glob("[0-9][0-9][0-9]_*.sql"))


SCHEMA_VERSION = max([1] + [version for version, _ in _migrations()])
MAIN_ADMIN_SINCE = 3            # the schema version that introduced the main administrator

DEMO_USERS = [
    # username, display name, role, password
    ("admin", "Asha Iyer (Treasury Admin)", "admin", "LedgerAdmin#2026"),
    ("auditor", "Arjun Mehta (Auditor)", "auditor", "LedgerAudit#2026"),
    ("priya", "Priya Sharma", "holder", "LedgerPriya#2026"),
    ("rahul", "Rahul Verma", "holder", "LedgerRahul#2026"),
    ("acme", "Acme Supplies Pvt Ltd", "holder", "LedgerAcme#2026"),
]
# Made-up personal details for the demo holders (example.com addresses, no real people).
DEMO_DETAILS = {
    "priya": {"date_of_birth": date(1996, 4, 12), "mobile": "9876543210", "email": "priya.sharma@example.com",
              "address_line1": "Flat 302, Lakeview Residency", "address_line2": "Madhapur", "city": "Hyderabad",
              "state": "Telangana", "pin_code": "500081", "occupation": "salaried",
              "nominee_name": "Rakesh Sharma", "nominee_relation": "father"},
    "rahul": {"date_of_birth": date(1993, 9, 21), "mobile": "9123456780", "email": "rahul.verma@example.com",
              "address_line1": "12-4-56, MG Road", "address_line2": None, "city": "Vijayawada",
              "state": "Andhra Pradesh", "pin_code": "520010", "occupation": "self_employed",
              "nominee_name": "Anita Verma", "nominee_relation": "spouse"},
}


def migrate() -> None:
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            exists = conn.execute("SELECT to_regclass('ledgerline.schema_version') IS NOT NULL AS e").fetchone()["e"]
            if exists:
                version = conn.execute("SELECT max(version) AS v FROM ledgerline.schema_version").fetchone()["v"]
                pending = [(v, path) for v, path in _migrations() if v > version]
                if not pending:
                    print(f"schema already at version {version}")
                    return
                # An existing database: apply only what it lacks, all or nothing.
                with conn.transaction():
                    for _, path in pending:
                        conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute("SET search_path TO ledgerline, public")
                    switch = None
                    if version < MAIN_ADMIN_SINCE <= pending[-1][0]:
                        switch = _record_main_admin(conn.cursor())
                print(f"schema upgraded from version {version} to {pending[-1][0]}")
                if switch:
                    print(f"main administrator: {switch['details']['username'] or 'none (no active administrator)'}"
                          f" - signed entry #{switch['seq']}")
                return
            with conn.transaction():
                conn.execute(SCHEMA_FILE.read_text(encoding="utf-8"))
                for _, path in _migrations():
                    conn.execute(path.read_text(encoding="utf-8"))
                conn.execute("SET search_path TO ledgerline, public")
                cur = conn.cursor()
                cur.execute("SELECT id, account_no, kind, allow_negative FROM accounts ORDER BY id")
                system_accounts = [dict(r) for r in cur.fetchall()]
                signer = get_signer()
                append_entry(cur, kind="GENESIS", status="info", details={
                    "system": "LedgerLine",
                    "schema_version": SCHEMA_VERSION,
                    "invariant": "every posted entry's postings sum to zero, so the sum of all balances is always zero",
                    "money_unit": "INR paise (integer)",
                    "system_accounts": system_accounts,
                    "public_key": signer.public_key_b64,
                    "key_id": signer.key_id,
                })
            print(f"schema version {SCHEMA_VERSION} created; ledger key {get_signer().key_id}")
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


def _record_main_admin(cur) -> dict:
    """Upgrading across version 3 puts the switch on the signed record: who became
    the main administrator, and the entry from which only they manage
    administrators. Administrators created earlier were made under the earlier
    rule, and the proof judges them by it."""
    cur.execute("SELECT id, username FROM users WHERE is_main_admin")
    main = cur.fetchone()
    return append_entry(cur, kind="MAIN_ADMIN", status="info", details={
        "user_id": main["id"] if main else None,
        "username": main["username"] if main else None,
        "schema_version": MAIN_ADMIN_SINCE,
        "rule": "from this entry on, only the main administrator creates administrators or changes their access, "
                "and nobody can remove the main administrator",
    })


def _user_id(username: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()["id"]


def seed(if_empty: bool = False) -> None:
    with db.connect() as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK,))
        try:
            n = conn.execute("SELECT count(*) AS n FROM users").fetchone()["n"]
            if n and if_empty:
                print("users exist; seed skipped")
                return
            if n:
                print("refusing to seed a database that already has users (use --if-empty or reset)")
                sys.exit(1)
            _seed_demo()
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK,))


def _seed_demo() -> None:
    # The first administrator comes from this command line (no actor); everyone
    # else is created by that administrator, on the record.
    first = None
    for username, name, role, password in DEMO_USERS:
        created = admin_ops.create_user(first, username, name, role, password)
        first = first or created["id"]
    ids = {u[0]: _user_id(u[0]) for u in DEMO_USERS}
    admin = engine.Actor(ids["admin"], "admin", "admin")
    holder = {u: engine.Actor(ids[u], u, "holder") for u in ("priya", "rahul", "acme")}

    accts = {
        "priya_salary": admin_ops.open_account(admin.id, "priya", "Salary account")["account_no"],
        "priya_savings": admin_ops.open_account(admin.id, "priya", "Savings")["account_no"],
        "rahul": admin_ops.open_account(admin.id, "rahul", "Everyday account")["account_no"],
        "acme": admin_ops.open_account(admin.id, "acme", "Vendor receivables")["account_no"],
    }
    rupees = 100
    step = iter(range(1, 1000))

    def key() -> str:
        return f"seed-{next(step):04d}"

    engine.deposit(admin, accts["priya_salary"], 50_000 * rupees, "November salary", key())
    engine.deposit(admin, accts["priya_savings"], 1_20_000 * rupees, "Opening balance", key())
    engine.deposit(admin, accts["rahul"], 25_000 * rupees, "November salary", key())
    engine.deposit(admin, accts["acme"], 3_00_000 * rupees, "Customer receipts", key())

    # Ordinary history, so Sentinel has a baseline for each account.
    for amount, to, memo in [(1_200, "rahul", "Dinner split"), (2_500, "acme", "Invoice INV-1041"),
                             (800, "rahul", "Cab share"), (1_900, "acme", "Invoice INV-1052"),
                             (650, "rahul", "Coffee"), (3_000, "acme", "Invoice INV-1060")]:
        engine.transfer(holder["priya"], accts["priya_salary"], accts[to], amount * rupees, memo, key())
    engine.transfer(holder["rahul"], accts["rahul"], accts["priya_salary"], 1_100 * rupees, "Movie tickets", key())
    engine.withdraw(holder["rahul"], accts["rahul"], 2_000 * rupees, "ATM", key())
    for amount, memo in [(12_000, "Refund INV-0998"), (9_500, "Supplier credit")]:
        engine.transfer(holder["acme"], accts["acme"], accts["priya_salary"], amount * rupees, memo, key())

    # One payment Sentinel holds for review, so the admin queue is not empty on day one.
    engine.transfer(holder["acme"], accts["acme"], accts["rahul"], 2_80_000 * rupees, "Urgent advance", key())
    # Personal details (made up, through the real path): Priya's are verified,
    # Rahul's wait for an administrator to check them.
    for username, values in DEMO_DETAILS.items():
        details.submit(ids[username], values)
    details.decide(admin.id, "priya", 1, True, None)
    print("seeded demo data:")
    for username, _, role, password in DEMO_USERS:
        print(f"  {role:8s} {username:8s} {password}")
    for label, number in accts.items():
        print(f"  account  {label:14s} {number}")


def verify() -> int:
    with db.connect() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        report = proof.run_proof(cur)
    for check in report["checks"]:
        mark = "PASS" if check["ok"] else "FAIL"
        print(f"[{mark}] {check['title']}" + (f" - {check['summary']}" if check["summary"] else ""))
        for problem in check["problems"]:
            print(f"        {problem}")
    print(f"entries={report['entries']} head=#{report['head']['seq']} {report['head']['hash'][:16]}... "
          f"key={report['key_id']}")
    return 0 if report["ok"] else 1


def export(path: str) -> None:
    with db.connect() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        bundle = proof.export_bundle(cur)
    Path(path).write_text(json.dumps(bundle, indent=1), encoding="utf-8")
    print(f"wrote {len(bundle['entries'])} entries to {path}")


def reset() -> None:
    if get_settings().is_production:
        print("refusing to reset a production database")
        sys.exit(1)
    with db.connect() as conn:
        conn.execute("DROP SCHEMA IF EXISTS ledgerline CASCADE")
    print("schema dropped")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate")
    seed_p = sub.add_parser("seed")
    seed_p.add_argument("--if-empty", action="store_true")
    sub.add_parser("verify")
    export_p = sub.add_parser("export")
    export_p.add_argument("file")
    sub.add_parser("keygen")
    reset_p = sub.add_parser("reset")
    reset_p.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    finally:
        db.close_pool()


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "migrate":
        migrate()
    elif args.command == "seed":
        seed(args.if_empty)
    elif args.command == "verify":
        return verify()
    elif args.command == "export":
        export(args.file)
    elif args.command == "keygen":
        print(secrets.token_urlsafe(48))
    elif args.command == "reset":
        if not args.yes:
            print("add --yes to really drop everything")
            return 1
        reset()
    return 0


if __name__ == "__main__":
    sys.exit(main())
