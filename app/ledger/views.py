"""JSON shapes returned by the API for journal entries, receipts and movements."""

from __future__ import annotations

from ..money import money_view
from ..signing import iso_utc

HOLDER_HOLD_MESSAGE = ("This payment is waiting for review by an administrator. The money is reserved "
                       "and has left your available balance; it is paid only if a person approves it, "
                       "and returned to you if they reject it.")


def receipt(entry: dict) -> dict:
    """Proof the server committed to this entry: anyone with the public key can
    check the signature, and the hash pins the entire history before it."""
    return {
        "seq": entry["seq"],
        "entry_id": str(entry["entry_id"]),
        "created_at": iso_utc(entry["created_at"]),
        "hash": entry["hash"],
        "prev_hash": entry["prev_hash"],
        "key_id": entry["key_id"],
        "signature": entry["signature"],
    }


def movement_body(result: str, *, kind: str, entry: dict, account: dict, counterparty: dict | None,
                  amount: int, memo: str | None, visible_balances: dict[str, int],
                  error: dict | None = None, review: dict | None = None, ref_seq: int | None = None) -> dict:
    body = {
        "status": result,                      # posted | held | refused
        "entry": {
            "seq": entry["seq"],
            "kind": kind,
            "created_at": iso_utc(entry["created_at"]),
            "account": account["account_no"],
            "counterparty": counterparty["account_no"] if counterparty else None,
            "amount": money_view(amount),
            "memo": memo,
            "ref_seq": ref_seq,
        },
        "receipt": receipt(entry),
    }
    if visible_balances:
        body["balances"] = {no: money_view(value) for no, value in sorted(visible_balances.items())}
    if error:
        body["error"] = error
    if review:
        body["review"] = review
    return body
