"""Review queue, audit, proof and evidence endpoints (admin + auditor, read-mostly)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response

from .. import db
from ..errors import Forbidden, NotFound
from ..ledger import ai_notes, proof, statements
from ..money import money_view
from ..signing import get_signer, iso_utc
from .deps import CurrentUser, current_user, require_roles

router = APIRouter(prefix="/api", tags=["review & audit"])
staff = require_roles("admin", "auditor")


def _names(cur, account_ids: set[int], user_ids: set[int]) -> tuple[dict, dict]:
    accts, users = {}, {}
    if account_ids:
        cur.execute("""SELECT a.id, a.account_no, a.name, u.display_name AS owner FROM accounts a
                       LEFT JOIN users u ON u.id = a.owner_id WHERE a.id = ANY(%s)""", (list(account_ids),))
        accts = {r["id"]: r for r in cur.fetchall()}
    if user_ids:
        cur.execute("SELECT id, username, display_name, role FROM users WHERE id = ANY(%s)", (list(user_ids),))
        users = {r["id"]: r for r in cur.fetchall()}
    return accts, users


def entry_view(row: dict, accts: dict, users: dict, *, full: bool = True) -> dict:
    def acct(i):
        a = accts.get(i)
        return {"account_no": a["account_no"], "owner": a["owner"] or a["name"]} if a else None

    actor = users.get(row["actor_id"])
    view = {
        "seq": row["seq"],
        "created_at": iso_utc(row["created_at"]),
        "kind": row["kind"],
        "status": row["status"],
        "actor": {"username": actor["username"], "name": actor["display_name"], "role": actor["role"]} if actor else None,
        "account": acct(row["account_id"]),
        "counterparty": acct(row["counterparty_id"]),
        "amount": money_view(row["amount"]),
        "reason_code": row["reason_code"],
        "memo": row["memo"],
        "ref_seq": row["ref_seq"],
        "idempotency_key": row["idempotency_key"],
        "hash": row["hash"],
        "prev_hash": row["prev_hash"],
        "key_id": row["key_id"],
        "signature": row["signature"],
    }
    if full:
        view["details"] = row["details"]
        view["observed_balance"] = money_view(row["observed_balance"])
    return view


# --------------------------------------------------------------------------- #
# Review queue                                                                 #
# --------------------------------------------------------------------------- #

@router.get("/reviews")
def reviews(state: str = Query("pending", pattern="^(pending|decided|all)$"), user: CurrentUser = Depends(staff)):
    with db.read_transaction(snapshot=True) as cur:
        cur.execute("SELECT * FROM journal WHERE kind = 'HOLD' AND status = 'posted' ORDER BY seq DESC LIMIT 200")
        holds = cur.fetchall()
        states = statements.hold_states(cur, [h["seq"] for h in holds])
        ids = {h["account_id"] for h in holds} | {h["details"]["destination_account_id"] for h in holds}
        accts, users = _names(cur, ids, {h["actor_id"] for h in holds})
        cur.execute("SELECT * FROM journal WHERE kind = 'AI_NOTE' AND ref_seq = ANY(%s) ORDER BY seq",
                    ([h["seq"] for h in holds],))
        notes = {}
        for n in cur.fetchall():
            notes[n["ref_seq"]] = {"seq": n["seq"], "text": n["details"].get("text"),
                                   "source": n["details"].get("source"), "model": n["details"].get("model"),
                                   "grounded": n["details"].get("grounded"),
                                   "rejected_numbers": n["details"].get("rejected_numbers", [])}
        out = []
        for h in holds:
            st = states[h["seq"]]
            if state != "all" and (st["state"] == "pending") != (state == "pending"):
                continue
            view = entry_view(h, accts, users)
            dest = accts.get(h["details"]["destination_account_id"])
            view["destination"] = {"account_no": dest["account_no"], "owner": dest["owner"] or dest["name"]} if dest else None
            view["intended_kind"] = h["details"].get("intended_kind")
            view["risk"] = h["details"].get("risk")
            view["state"] = st
            view["ai_note"] = notes.get(h["seq"])
            out.append(view)
        return {"reviews": out}


@router.post("/reviews/{hold_seq}/ai-note")
def ai_note(hold_seq: int, user: CurrentUser = Depends(staff)):
    return ai_notes.note_for_hold(user.id, hold_seq)


# --------------------------------------------------------------------------- #
# Audit                                                                        #
# --------------------------------------------------------------------------- #

@router.get("/audit/proof")
def run_proof(user: CurrentUser = Depends(staff)):
    with db.read_transaction(snapshot=True) as cur:
        return proof.run_proof(cur)


@router.post("/audit/reconciliation")
def reconciliation(user: CurrentUser = Depends(staff)):
    return ai_notes.reconciliation(user.id)


@router.get("/audit/journal")
def journal(user: CurrentUser = Depends(staff), limit: int = Query(100, ge=1, le=500),
            before_seq: int | None = Query(None, gt=0), kind: str | None = None,
            status: str | None = Query(None, pattern="^(posted|refused|info)$"), account: str | None = None):
    clauses, params = [], []
    if before_seq:
        clauses.append("j.seq < %s")
        params.append(before_seq)
    if kind:
        clauses.append("j.kind = %s")
        params.append(kind.upper())
    if status:
        clauses.append("j.status = %s")
        params.append(status)
    if account:
        clauses.append("(a.account_no = %s OR c.account_no = %s)")
        params += [account.strip().upper()] * 2
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    with db.read_transaction(snapshot=True) as cur:
        cur.execute(f"""SELECT j.* FROM journal j
                        LEFT JOIN accounts a ON a.id = j.account_id
                        LEFT JOIN accounts c ON c.id = j.counterparty_id
                        {where} ORDER BY j.seq DESC LIMIT %s""", (*params, limit))
        rows = cur.fetchall()
        accts, users = _names(cur, {r["account_id"] for r in rows if r["account_id"]} |
                              {r["counterparty_id"] for r in rows if r["counterparty_id"]},
                              {r["actor_id"] for r in rows if r["actor_id"]})
        return {"entries": [entry_view(r, accts, users) for r in rows],
                "next_before_seq": rows[-1]["seq"] if len(rows) == limit else None}


@router.get("/entries/{seq}")
def entry(seq: int, user: CurrentUser = Depends(current_user)):
    """One entry with its own hash + signature check. Holders may check entries
    that touch their accounts (receipt verification)."""
    with db.read_transaction(snapshot=True) as cur:
        cur.execute("SELECT * FROM journal WHERE seq = %s", (seq,))
        row = cur.fetchone()
        if row is None:
            raise NotFound(f"no journal entry #{seq}")
        if user.role == "holder":
            cur.execute("""SELECT EXISTS (SELECT 1 FROM accounts a WHERE a.owner_id = %s AND a.id IN (
                               SELECT account_id FROM postings WHERE entry_seq = %s
                               UNION SELECT %s::bigint UNION SELECT %s::bigint)) AS mine""",
                        (user.id, seq, row["account_id"], row["counterparty_id"]))
            if not cur.fetchone()["mine"]:
                raise Forbidden("this entry does not involve your accounts")
        check = proof.verify_single(cur, seq)
        accts, users = _names(cur, {x for x in (row["account_id"], row["counterparty_id"]) if x},
                              {row["actor_id"]} if row["actor_id"] else set())
        view = entry_view(row, accts, users, full=user.role != "holder")
        view["verification"] = {"hash_matches_content": check["hash_matches_content"],
                                "signature_valid": check["signature_valid"], "key_id": check["key_id"]}
        return view


@router.get("/audit/duplicates")
def duplicates(user: CurrentUser = Depends(staff), limit: int = Query(200, ge=1, le=1000)):
    with db.read_transaction() as cur:
        cur.execute("""SELECT r.id, r.received_at, r.idem_key, r.endpoint, r.outcome, r.original_entry_seq,
                              u.username, j.kind, j.status, j.amount
                         FROM idempotency_replays r JOIN users u ON u.id = r.user_id
                         LEFT JOIN journal j ON j.seq = r.original_entry_seq
                        ORDER BY r.id DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
        cur.execute("SELECT outcome, count(*) AS n FROM idempotency_replays GROUP BY outcome")
        totals = {r["outcome"]: r["n"] for r in cur.fetchall()}
    return {"totals": totals, "replays": [
        {"id": r["id"], "received_at": iso_utc(r["received_at"]), "user": r["username"], "key": r["idem_key"],
         "endpoint": r["endpoint"], "outcome": r["outcome"], "original_entry_seq": r["original_entry_seq"],
         "original": {"kind": r["kind"], "status": r["status"], "amount": money_view(r["amount"])}
         if r["kind"] else None} for r in rows]}


FLAG_QUERIES = {
    "held_payments": ("Held for review by Sentinel",
                      "SELECT * FROM journal WHERE kind = 'HOLD' AND status = 'posted'"),
    "elevated_risk": ("Paid, but with an elevated risk score (>= 30)",
                      "SELECT * FROM journal WHERE kind IN ('TRANSFER','WITHDRAWAL') AND status = 'posted' "
                      "AND (details->'risk'->>'score')::int >= 30"),
    "authorisation_refusals": ("Someone tried to move money they are not entitled to",
                               "SELECT * FROM journal WHERE status = 'refused' "
                               "AND reason_code IN ('NOT_ACCOUNT_OWNER', 'ROLE_NOT_PERMITTED')"),
    "backstops": ("The database had to refuse a write (should never happen)",
                  "SELECT * FROM journal WHERE reason_code = 'INTEGRITY_BACKSTOP'"),
    "reversals": ("Administrator reversals",
                  "SELECT * FROM journal WHERE kind = 'REVERSAL'"),
    "control_changes": ("Freezes, overdraft limits, access changes, auditor decisions and new administrators",
                        "SELECT * FROM journal WHERE kind IN ('ACCOUNT_STATUS','OVERDRAFT_LIMIT','USER_STATUS',"
                        "'ACCESS_DECISION','MAIN_ADMIN') OR (kind = 'USER_CREATED' AND details->>'role' = 'admin')"),
    "large_deposits": ("Deposits of Rs 5,00,000 or more",
                       "SELECT * FROM journal WHERE kind = 'DEPOSIT' AND status = 'posted' AND amount >= 50000000"),
}


@router.get("/audit/flags")
def flags(user: CurrentUser = Depends(staff)):
    out = {}
    with db.read_transaction(snapshot=True) as cur:
        for key, (title, sql) in FLAG_QUERIES.items():
            cur.execute(sql + " ORDER BY seq DESC LIMIT 50")
            rows = cur.fetchall()
            accts, users = _names(cur, {r["account_id"] for r in rows if r["account_id"]} |
                                  {r["counterparty_id"] for r in rows if r["counterparty_id"]},
                                  {r["actor_id"] for r in rows if r["actor_id"]})
            out[key] = {"title": title, "entries": [entry_view(r, accts, users) for r in rows]}
        cur.execute("SELECT count(*) AS n FROM idempotency_replays WHERE outcome = 'key_reused_with_different_request'")
        out["key_reuse"] = {"title": "Idempotency keys reused for a different request", "count": cur.fetchone()["n"]}
    return out


@router.get("/audit/export")
def export(user: CurrentUser = Depends(staff)):
    with db.read_transaction(snapshot=True) as cur:
        bundle = proof.export_bundle(cur)
    return Response(content=json.dumps(bundle, indent=1), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="ledgerline-evidence.json"'})


@router.get("/public-key")
def public_key():
    signer = get_signer()
    return {"algorithm": "Ed25519", "key_id": signer.key_id, "public_key": signer.public_key_b64,
            "signs": "sha256 of the canonical JSON body of each journal entry (see tools/verify_bundle.py)"}
