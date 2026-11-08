"""Proof that the books balance and the record is intact.

run_proof() replays the WHOLE journal from entry #1 inside one REPEATABLE READ
snapshot and checks, independently of the cached balances:

  chain        every entry's hash recomputes, links to the previous one, seq is gapless
  signatures   every entry hash carries a valid Ed25519 signature from our key
  head         the chain head row points at the last entry
  structure    posted entries: >= 2 postings, sum 0, credits == amount; others: none
  replay       re-adding postings from zero reproduces every balance_after
  floors       no customer balance ever went below its authorised floor at the time;
               suspense never below zero
  cached       accounts.balance == sum of that account's postings
  conservation sum of ALL balances == 0, and money inside LedgerLine (customer accounts +
               suspense) == external money in - external money out
  suspense     suspense balance == sum of payments still waiting for review
  decisions    no held payment decided twice; no entry reversed twice
  exactly-once no idempotency key appears on two journal entries
  refusals     each refused attempt's recorded balance equals the replayed balance
  access       every user has exactly one USER_CREATED entry, with the role it has now;
               auditors and admins were created by another administrator (or the
               server's command line); no access request decided twice, and every
               approval created its auditor
  backstop     how many times the database had to refuse a write (should be 0)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone

from psycopg import Cursor

from ..money import money_view
from ..signing import GENESIS_PREV_HASH, body_hash, entry_body, get_signer, iso_utc, verify_signature

BATCH = 2000
MAX_PROBLEMS = 25


def iter_entries(cur: Cursor):
    """Yield (journal row, postings) in sequence order, in batches."""
    last = 0
    while True:
        cur.execute("SELECT * FROM journal WHERE seq > %s ORDER BY seq LIMIT %s", (last, BATCH))
        rows = cur.fetchall()
        if not rows:
            return
        cur.execute(
            "SELECT entry_seq, line_no, account_id, amount, balance_after FROM postings "
            "WHERE entry_seq BETWEEN %s AND %s ORDER BY entry_seq, line_no",
            (rows[0]["seq"], rows[-1]["seq"]))
        by_entry = defaultdict(list)
        for p in cur.fetchall():
            by_entry[p["entry_seq"]].append(p)
        for row in rows:
            yield row, by_entry.get(row["seq"], [])
        last = rows[-1]["seq"]


class _Check:
    def __init__(self, check_id: str, title: str):
        self.id, self.title = check_id, title
        self.problems: list[str] = []
        self.count = 0
        self.summary = ""

    def fail(self, message: str) -> None:
        self.count += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(message)

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "ok": self.count == 0, "failures": self.count,
                "summary": self.summary, "problems": self.problems}


def run_proof(cur: Cursor) -> dict:
    signer = get_signer()
    checks = {c[0]: _Check(*c) for c in [
        ("chain", "Hash chain intact (every entry recomputes and links to the one before)"),
        ("signatures", "Every entry signed by the ledger key"),
        ("head", "Chain head points at the last entry"),
        ("structure", "Double entry: posted entries balance to zero; refusals move nothing"),
        ("replay", "Replaying postings from zero reproduces every running balance"),
        ("floors", "No balance ever went below its floor"),
        ("cached", "Stored balances equal the sum of their postings"),
        ("conservation", "Money is conserved"),
        ("suspense", "Suspense holds exactly the payments under review"),
        ("decisions", "No held payment decided twice, no entry reversed twice"),
        ("exactly_once", "No idempotency key moved money twice"),
        ("refusals", "Refused attempts recorded the true balance at that moment"),
        ("access", "Every user is on the record with their role; staff access only from an administrator"),
        ("backstop", "Database backstop never had to fire"),
    ]}

    cur.execute("SELECT id, account_no, kind, name, balance, overdraft_limit, allow_negative FROM accounts")
    accounts = {row["id"]: row for row in cur.fetchall()}
    external = next((a for a in accounts.values() if a["account_no"] == "SYS-EXTERNAL"), None)
    suspense = next((a for a in accounts.values() if a["account_no"] == "SYS-SUSPENSE"), None)

    expected_seq, expected_prev = 1, GENESIS_PREV_HASH
    running: dict[int, int] = defaultdict(int)
    floor_limit: dict[int, int] = defaultdict(int)          # overdraft limit in force, replayed
    pending: dict[int, int] = {}
    decided: set[int] = set()
    reversed_: set[int] = set()
    keys: Counter = Counter()
    kinds: Counter = Counter()
    statuses: Counter = Counter()
    external_in = external_out = 0
    last_row = None
    entries = 0
    user_created: dict[int, list[dict]] = defaultdict(list)
    access_decisions: dict = {}

    for row, postings in iter_entries(cur):
        entries += 1
        last_row = row
        seq = row["seq"]
        kinds[row["kind"]] += 1
        statuses[row["status"]] += 1

        # --- chain + signature ---
        if seq != expected_seq:
            checks["chain"].fail(f"gap: expected entry #{expected_seq}, found #{seq}")
        if row["prev_hash"] != expected_prev:
            checks["chain"].fail(f"#{seq}: prev_hash does not match the hash of the entry before it")
        recomputed = body_hash(entry_body(row, postings))
        if recomputed != row["hash"]:
            checks["chain"].fail(f"#{seq}: content does not match its hash (row was altered)")
        if row["key_id"] != signer.key_id or not verify_signature(signer.public_bytes, row["hash"], row["signature"]):
            checks["signatures"].fail(f"#{seq}: signature does not verify")
        if recomputed != row["hash"] and verify_signature(signer.public_bytes, recomputed, row["signature"]):
            checks["chain"].fail(f"#{seq}: stored hash was rewritten but the signed content was not")
        expected_seq, expected_prev = seq + 1, row["hash"]

        # --- structure ---
        total = sum(p["amount"] for p in postings)
        credits = sum(p["amount"] for p in postings if p["amount"] > 0)
        if row["status"] == "posted":
            if len(postings) < 2 or total != 0 or credits != row["amount"]:
                checks["structure"].fail(f"#{seq}: posted {row['kind']} lines={len(postings)} sum={total} "
                                         f"credits={credits} amount={row['amount']}")
        elif postings:
            checks["structure"].fail(f"#{seq}: {row['status']} entry carries postings")

        # --- refused attempts saw the true balance ---
        if row["status"] == "refused" and row["observed_balance"] is not None:
            observed_id = row["details"].get("observed_account_id", row["account_id"])
            if running[observed_id] != row["observed_balance"]:
                checks["refusals"].fail(f"#{seq}: recorded balance {row['observed_balance']} but replay says "
                                        f"{running[observed_id]}")
        if row["reason_code"] == "INTEGRITY_BACKSTOP":
            checks["backstop"].fail(f"#{seq}: database refused a {row['kind']} ({row['details'].get('sqlstate')})")

        # --- replay balances and floors ---
        for p in postings:
            acct_id = p["account_id"]
            running[acct_id] += p["amount"]
            if running[acct_id] != p["balance_after"]:
                checks["replay"].fail(f"#{seq}: account {acct_id} replays to {running[acct_id]}, "
                                      f"row says {p['balance_after']}")
            acct = accounts.get(acct_id)
            if acct is None:
                checks["structure"].fail(f"#{seq}: posting to unknown account {acct_id}")
                continue
            if not acct["allow_negative"] and p["balance_after"] < -floor_limit[acct_id]:
                checks["floors"].fail(f"#{seq}: {acct['account_no']} went to {p['balance_after']} "
                                      f"(floor {-floor_limit[acct_id]})")
            if external and acct_id == external["id"]:
                if p["amount"] < 0:
                    external_in += -p["amount"]
                else:
                    external_out += p["amount"]

        if row["kind"] == "OVERDRAFT_LIMIT":
            floor_limit[row["account_id"]] = int(row["details"].get("to", 0))

        # --- decisions / reversals ---
        if row["status"] == "posted":
            if row["kind"] == "HOLD":
                pending[seq] = row["amount"]
            elif row["kind"] in ("HOLD_APPROVED", "HOLD_REJECTED"):
                if row["ref_seq"] in decided:
                    checks["decisions"].fail(f"hold #{row['ref_seq']} decided twice (again by #{seq})")
                decided.add(row["ref_seq"])
                pending.pop(row["ref_seq"], None)
            elif row["kind"] == "REVERSAL":
                if row["ref_seq"] in reversed_:
                    checks["decisions"].fail(f"entry #{row['ref_seq']} reversed twice (again by #{seq})")
                reversed_.add(row["ref_seq"])
        if row["idempotency_key"]:
            keys[(row["actor_id"], row["idempotency_key"])] += 1

        # --- who may sign in ---
        if row["kind"] == "USER_CREATED":
            user_created[int(row["details"].get("user_id") or 0)].append(row)
        elif row["kind"] == "ACCESS_DECISION":
            request_id = row["details"].get("request_id")
            if request_id in access_decisions:
                checks["access"].fail(f"access request {request_id} decided twice (again by #{seq})")
            access_decisions[request_id] = row

    # --- head ---
    cur.execute("SELECT last_seq, last_hash FROM ledger_head WHERE id = 1")
    head = cur.fetchone()
    if last_row is None:
        checks["head"].fail("journal is empty")
    elif head["last_seq"] != last_row["seq"] or head["last_hash"] != last_row["hash"]:
        checks["head"].fail(f"head says #{head['last_seq']}, journal ends at #{last_row['seq']}")

    # --- cached balances ---
    for acct in accounts.values():
        if acct["balance"] != running.get(acct["id"], 0):
            checks["cached"].fail(f"{acct['account_no']}: stored {acct['balance']}, postings sum to "
                                  f"{running.get(acct['id'], 0)}")

    # --- conservation ---
    total_all = sum(a["balance"] for a in accounts.values())
    inside = sum(a["balance"] for a in accounts.values() if a["kind"] == "customer")
    inside += suspense["balance"] if suspense else 0
    net_external = external_in - external_out
    if total_all != 0:
        checks["conservation"].fail(f"sum of all balances is {total_all}, not 0")
    if inside != net_external:
        checks["conservation"].fail(f"money inside is {inside} but external in - out is {net_external}")
    checks["conservation"].summary = (
        f"sum of all balances = {total_all}; inside LedgerLine {money_view(inside)['display']} = "
        f"in {money_view(external_in)['display']} - out {money_view(external_out)['display']}")

    held_total = sum(pending.values())
    if suspense and suspense["balance"] != held_total:
        checks["suspense"].fail(f"suspense holds {suspense['balance']} but pending reviews total {held_total}")
    checks["suspense"].summary = f"{len(pending)} payment(s) under review, {money_view(held_total)['display']}"

    for (actor_id, key), n in keys.items():
        if n > 1:
            checks["exactly_once"].fail(f"user {actor_id} key {key} appears on {n} entries")

    cur.execute("SELECT outcome, count(*) AS n FROM idempotency_replays GROUP BY outcome")
    replays = {row["outcome"]: row["n"] for row in cur.fetchall()}
    checks["exactly_once"].summary = (
        f"{sum(1 for _ in keys)} keyed requests; duplicates answered from storage: {replays.get('replayed', 0)}; "
        f"key reuse refused: {replays.get('key_reused_with_different_request', 0)}; "
        f"in-flight retries: {replays.get('in_flight_timeout', 0)}")
    _check_access(cur, checks["access"], user_created, access_decisions)
    checks["chain"].summary = f"{entries} entries, #1 .. #{last_row['seq'] if last_row else 0}"
    checks["signatures"].summary = f"key {signer.key_id}"
    checks["backstop"].summary = "the application's own checks stopped every bad request"

    views = [c.view() for c in checks.values()]
    return {
        "ok": all(v["ok"] for v in views),
        "checked_at": iso_utc(datetime.now(timezone.utc)),
        "entries": entries,
        "head": {"seq": head["last_seq"], "hash": head["last_hash"]},
        "key_id": signer.key_id,
        "public_key": signer.public_key_b64,
        "totals": {
            "inside_ledgerline": money_view(inside),
            "external_in": money_view(external_in),
            "external_out": money_view(external_out),
            "sum_of_all_balances": total_all,
            "under_review": money_view(held_total),
            "pending_reviews": len(pending),
        },
        "counts": {"by_kind": dict(kinds), "by_status": dict(statuses), "replays": replays},
        "checks": views,
    }


def _check_access(cur: Cursor, check: _Check, user_created: dict[int, list[dict]], decisions: dict) -> None:
    """The users table must agree with the signed record of how each user came to be."""
    cur.execute("SELECT id, username, role FROM users")
    users = {row["id"]: row for row in cur.fetchall()}
    for user_id, user in users.items():
        made = user_created.get(user_id, [])
        if len(made) != 1:
            check.fail(f"user {user['username']} has {len(made)} USER_CREATED entries, expected exactly 1")
            continue
        entry = made[0]
        if entry["details"].get("role") != user["role"]:
            check.fail(f"user {user['username']} is {user['role']} but was created as "
                       f"{entry['details'].get('role')} (#{entry['seq']})")
        if user["role"] != "holder" and entry["actor_id"] is not None:
            creator = users.get(entry["actor_id"])
            if creator is None or creator["role"] != "admin" or creator["id"] == user_id:
                check.fail(f"{user['role']} {user['username']} was not created by an administrator (#{entry['seq']})")
    for user_id, made in user_created.items():
        if user_id not in users:
            check.fail(f"#{made[0]['seq']} records user {user_id}, who does not exist")
    created_from = {e["ref_seq"]: e for made in user_created.values() for e in made if e["ref_seq"]}
    approved = [d for d in decisions.values() if d["details"].get("decision") == "approved"]
    for decision in approved:
        made = created_from.get(decision["seq"])
        if made is None or made["details"].get("role") != decision["details"].get("role") \
                or made["details"].get("username") != decision["details"].get("username"):
            check.fail(f"approval #{decision['seq']} did not create the auditor it approved")
    staff = sum(1 for user in users.values() if user["role"] != "holder")
    check.summary = (f"{len(users)} users, {staff} with staff access; auditor requests approved: {len(approved)}, "
                     f"declined: {len(decisions) - len(approved)}")


def verify_single(cur: Cursor, seq: int) -> dict | None:
    """Check one entry's hash and signature (a receipt check)."""
    cur.execute("SELECT * FROM journal WHERE seq = %s", (seq,))
    row = cur.fetchone()
    if row is None:
        return None
    cur.execute("SELECT entry_seq, line_no, account_id, amount, balance_after FROM postings "
                "WHERE entry_seq = %s ORDER BY line_no", (seq,))
    postings = cur.fetchall()
    signer = get_signer()
    body = entry_body(row, postings)
    recomputed = body_hash(body)
    return {
        "seq": seq,
        "hash": row["hash"],
        "hash_matches_content": recomputed == row["hash"],
        "signature_valid": verify_signature(signer.public_bytes, row["hash"], row["signature"]),
        "key_id": row["key_id"],
        "body": body,
    }


def export_bundle(cur: Cursor) -> dict:
    """Everything needed to re-verify the ledger offline with only the public key."""
    signer = get_signer()
    entries = []
    for row, postings in iter_entries(cur):
        entries.append({"body": entry_body(row, postings), "hash": row["hash"], "key_id": row["key_id"],
                        "signature": row["signature"]})
    return {
        "format": "ledgerline-evidence/v1",
        "exported_at": iso_utc(datetime.now(timezone.utc)),
        "public_key": signer.public_key_b64,
        "key_id": signer.key_id,
        "entries": entries,
    }
