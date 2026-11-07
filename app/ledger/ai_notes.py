"""Putting AI suggestions on the record.

The LLM is called with NO database transaction open (a slow provider can never
hold a lock that payments need). Its note is then journaled as an AI_NOTE info
entry: hash-chained and signed like everything else, carrying the model name,
the facts hash, whether the text passed the grounding check, and who asked.
"""

from __future__ import annotations

from .. import db
from ..errors import NotFound
from ..sentinel import narrator
from ..signing import iso_utc
from . import proof
from .journal import append_entry


def _note_view(seq: int, created_at, details: dict, cached: bool) -> dict:
    return {
        "seq": seq,
        "created_at": iso_utc(created_at),
        "cached": cached,
        "text": details.get("text"),
        "source": details.get("source"),
        "model": details.get("model"),
        "grounded": details.get("grounded"),
        "rejected_numbers": details.get("rejected_numbers", []),
        "llm_error": details.get("llm_error"),
        "facts_sha256": details.get("facts_sha256"),
    }


def _cached(cur, subject: str, ref_seq: int | None, digest: str) -> dict | None:
    cur.execute(
        """SELECT seq, created_at, details FROM journal
            WHERE kind = 'AI_NOTE' AND details->>'subject' = %s AND details->>'facts_sha256' = %s
              AND ref_seq IS NOT DISTINCT FROM %s
            ORDER BY seq DESC LIMIT 1""", (subject, digest, ref_seq))
    return cur.fetchone()


def note_for_hold(actor_id: int, hold_seq: int) -> dict:
    with db.read_transaction() as cur:
        cur.execute("SELECT * FROM journal WHERE seq = %s", (hold_seq,))
        hold = cur.fetchone()
        if hold is None or hold["kind"] != "HOLD" or hold["status"] != "posted":
            raise NotFound(f"no held payment #{hold_seq}")
        cur.execute("SELECT id, account_no FROM accounts WHERE id = ANY(%s)",
                    ([hold["account_id"], hold["details"]["destination_account_id"]],))
        numbers = {row["id"]: row["account_no"] for row in cur.fetchall()}
        facts = narrator.hold_facts(hold, numbers[hold["account_id"]],
                                    numbers[hold["details"]["destination_account_id"]])
        hit = _cached(cur, "hold", hold_seq, narrator.facts_hash(facts))
        if hit:
            return _note_view(hit["seq"], hit["created_at"], hit["details"], cached=True)

    note = narrator.explain_hold(facts)          # no transaction, no locks held here

    with db.connection() as conn, conn.transaction():
        entry = append_entry(conn.cursor(), kind="AI_NOTE", status="info", actor_id=actor_id,
                             ref_seq=hold_seq, details={"subject": "hold", **note.record()})
    return _note_view(entry["seq"], entry["created_at"], entry["details"], cached=False)


def reconciliation(actor_id: int) -> dict:
    with db.read_transaction(snapshot=True) as cur:
        report = proof.run_proof(cur)
        facts = narrator.reconciliation_facts(report)
        hit = _cached(cur, "reconciliation", None, narrator.facts_hash(facts))
    if hit:
        return {"report": report, "note": _note_view(hit["seq"], hit["created_at"], hit["details"], cached=True)}

    note = narrator.summarize_reconciliation(facts)

    with db.connection() as conn, conn.transaction():
        entry = append_entry(conn.cursor(), kind="AI_NOTE", status="info", actor_id=actor_id,
                             details={"subject": "reconciliation", "facts": facts, **note.record()})
    return {"report": report, "note": _note_view(entry["seq"], entry["created_at"], entry["details"], cached=False)}
