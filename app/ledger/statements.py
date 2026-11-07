"""Per-account statements and journal browsing.

A statement row is either a posting on the account (a debit or credit, with the
running balance the database computed) or a refused attempt against the
account (with the balance observed at that moment). Rows are ordered by the
journal sequence number, which is the order in which things really happened.
"""

from __future__ import annotations

from psycopg import Cursor

from ..money import money_view
from ..signing import iso_utc

KIND_LABEL = {
    "DEPOSIT": "Deposit",
    "WITHDRAWAL": "Withdrawal",
    "TRANSFER": "Transfer",
    "HOLD": "Payment held for review",
    "HOLD_APPROVED": "Approved payment",
    "HOLD_REJECTED": "Rejected payment returned",
    "REVERSAL": "Reversal",
}


def _accounts_by_id(cur: Cursor, ids: set[int]) -> dict[int, dict]:
    if not ids:
        return {}
    cur.execute(
        """SELECT a.id, a.account_no, a.name, a.kind, u.display_name AS owner_name
             FROM accounts a LEFT JOIN users u ON u.id = a.owner_id
            WHERE a.id = ANY(%s)""", (list(ids),))
    return {row["id"]: row for row in cur.fetchall()}


def hold_states(cur: Cursor, hold_seqs: list[int]) -> dict[int, dict]:
    if not hold_seqs:
        return {}
    cur.execute(
        """SELECT ref_seq, seq, kind FROM journal
            WHERE ref_seq = ANY(%s) AND kind IN ('HOLD_APPROVED', 'HOLD_REJECTED') AND status = 'posted'""",
        (hold_seqs,))
    found = {row["ref_seq"]: row for row in cur.fetchall()}
    states = {}
    for seq in hold_seqs:
        decided = found.get(seq)
        if decided is None:
            states[seq] = {"state": "pending", "decided_by_seq": None}
        else:
            states[seq] = {"state": "approved" if decided["kind"] == "HOLD_APPROVED" else "rejected",
                           "decided_by_seq": decided["seq"]}
    return states


def _party(acct: dict | None) -> dict | None:
    if acct is None:
        return None
    if acct["kind"] == "system":
        return {"account_no": acct["account_no"], "name": acct["name"]}
    return {"account_no": acct["account_no"], "name": acct["owner_name"] or acct["name"]}


def _describe(row: dict, other: dict | None, direction: str) -> str:
    kind, status = row["kind"], row["status"]
    other_label = other["account_no"] if other else "?"
    if status == "refused":
        verb = KIND_LABEL.get(kind, kind.title())
        return f"{verb} refused ({row['reason_code']})"
    if kind == "DEPOSIT":
        return "Deposit from outside" if direction == "credit" else "Deposit"
    if kind == "WITHDRAWAL":
        return "Withdrawal to outside"
    if kind == "TRANSFER":
        return f"Transfer to {other_label}" if direction == "debit" else f"Transfer from {other_label}"
    if kind == "HOLD":
        return f"Payment to {other_label} - held for review" if direction == "debit" \
            else f"Held payment from {other_label}"
    if kind == "HOLD_APPROVED":
        return f"Approved payment from {other_label}" if direction == "credit" \
            else f"Released approved payment to {other_label}"
    if kind == "HOLD_REJECTED":
        return f"Returned: payment to {other_label} was rejected" if direction == "credit" \
            else f"Returned rejected payment to {other_label}"
    if kind == "REVERSAL":
        return f"Reversal of entry #{row['ref_seq']}"
    return kind


def statement(cur: Cursor, account: dict, *, limit: int = 100, before_seq: int | None = None) -> list[dict]:
    limit = max(1, min(limit, 500))
    cur.execute(
        """SELECT * FROM (
             SELECT j.seq, j.created_at, j.kind, j.status, j.reason_code, j.memo, j.ref_seq, j.account_id,
                    j.counterparty_id, j.actor_id, j.hash, j.details, p.amount AS signed_amount,
                    p.balance_after AS balance
               FROM postings p JOIN journal j ON j.seq = p.entry_seq
              WHERE p.account_id = %(acct)s AND (%(before)s::bigint IS NULL OR j.seq < %(before)s)
             UNION ALL
             SELECT j.seq, j.created_at, j.kind, j.status, j.reason_code, j.memo, j.ref_seq, j.account_id,
                    j.counterparty_id, j.actor_id, j.hash, j.details,
                    CASE WHEN j.kind = 'DEPOSIT' THEN j.amount ELSE -j.amount END AS signed_amount,
                    j.observed_balance AS balance
               FROM journal j
              WHERE j.status = 'refused' AND j.account_id = %(acct)s
                AND (%(before)s::bigint IS NULL OR j.seq < %(before)s)
           ) s ORDER BY seq DESC LIMIT %(limit)s""",
        {"acct": account["id"], "before": before_seq, "limit": limit},
    )
    rows = cur.fetchall()
    ids: set[int] = set()
    actor_ids: set[int] = set()
    for row in rows:
        ids.update(x for x in (row["account_id"], row["counterparty_id"]) if x)
        if row["actor_id"]:
            actor_ids.add(row["actor_id"])
    by_id = _accounts_by_id(cur, ids)
    actors = {}
    if actor_ids:
        cur.execute("SELECT id, username, display_name, role FROM users WHERE id = ANY(%s)", (list(actor_ids),))
        actors = {row["id"]: row for row in cur.fetchall()}
    holds = hold_states(cur, [row["seq"] for row in rows if row["kind"] == "HOLD" and row["status"] == "posted"])

    out = []
    for row in rows:
        other_id = row["counterparty_id"] if row["account_id"] == account["id"] else row["account_id"]
        other = _party(by_id.get(other_id))
        direction = "credit" if row["signed_amount"] > 0 else "debit"
        actor = actors.get(row["actor_id"])
        out.append({
            "seq": row["seq"],
            "at": iso_utc(row["created_at"]),
            "kind": row["kind"],
            "status": row["status"],
            "direction": direction,
            "amount": money_view(abs(row["signed_amount"])),
            "balance_after": money_view(row["balance"]),
            "description": _describe(row, other, direction),
            "counterparty": other,
            "initiated_by": {"username": actor["username"], "name": actor["display_name"], "role": actor["role"]}
            if actor else None,
            "memo": row["memo"],
            "reason_code": row["reason_code"],
            "ref_seq": row["ref_seq"],
            "hold": holds.get(row["seq"]),
            "hash": row["hash"],
        })
    return out


def pending_holds_total(cur: Cursor, account_id: int) -> int:
    cur.execute(
        """SELECT COALESCE(sum(h.amount), 0)::bigint AS total FROM journal h
            WHERE h.kind = 'HOLD' AND h.status = 'posted' AND h.account_id = %s
              AND NOT EXISTS (SELECT 1 FROM journal d WHERE d.ref_seq = h.seq AND d.status = 'posted'
                               AND d.kind IN ('HOLD_APPROVED', 'HOLD_REJECTED'))""", (account_id,))
    return cur.fetchone()["total"]


def account_view(cur: Cursor, acct: dict, *, include_owner: bool = True) -> dict:
    view = {
        "account_no": acct["account_no"],
        "name": acct["name"],
        "kind": acct["kind"],
        "status": acct["status"],
        "currency": acct["currency"],
        "balance": money_view(acct["balance"]),
        "overdraft_limit": money_view(acct["overdraft_limit"]),
        "available": money_view(acct["balance"] + acct["overdraft_limit"]) if acct["kind"] == "customer" else None,
        "daily_limit": money_view(acct["daily_limit"]) if acct["kind"] == "customer" else None,
        "opened_at": iso_utc(acct["created_at"]),
    }
    if acct["kind"] == "customer":
        view["pending_review"] = money_view(pending_holds_total(cur, acct["id"]))
    if include_owner and acct.get("owner_id"):
        cur.execute("SELECT username, display_name FROM users WHERE id = %s", (acct["owner_id"],))
        owner = cur.fetchone()
        view["owner"] = {"username": owner["username"], "name": owner["display_name"]} if owner else None
    return view
