"""Appending to the hash-chained, signed, append-only journal.

append_entry() is the ONLY code path that writes journal rows and postings.
It must be called inside a transaction in which the caller already holds the
row locks (ledger.accounts.lock) for every customer account it posts to.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from psycopg import Cursor
from psycopg.types.json import Jsonb

from .. import hooks
from ..signing import body_hash, entry_body, get_signer


@dataclass(frozen=True)
class Line:
    """One double-entry line. ``account`` is the row as read by the caller
    (for customer accounts: read under the caller's row lock). ``amount`` is
    signed: negative debits the account, positive credits it."""

    account: dict
    amount: int


def append_entry(cur: Cursor, *, kind: str, status: str, actor_id: int | None = None,
                 account_id: int | None = None, counterparty_id: int | None = None,
                 amount: int | None = None, idempotency_key: str | None = None,
                 reason_code: str | None = None, memo: str | None = None,
                 ref_seq: int | None = None, details: dict | None = None,
                 lines: tuple[Line, ...] | list[Line] = (), observe_account_id: int | None = None) -> dict:
    # 1) Serialise appends: lock the single chain-head row. Everyone who writes
    #    the journal queues here, so sequence numbers are gapless and the hash
    #    chain is linear. Customer-account locks were taken BEFORE this point.
    cur.execute("SELECT last_seq, last_hash, last_at FROM ledger_head WHERE id = 1 FOR UPDATE")
    head = cur.fetchone()
    hooks.pause_point("after_chain_head", cur.connection)
    # One clock for every app instance: the database's. Never earlier than the head.
    cur.execute("SELECT GREATEST(clock_timestamp(), %s::timestamptz) AS now", (head["last_at"],))
    created_at = cur.fetchone()["now"]
    seq = head["last_seq"] + 1

    # 2) Work out each posting's resulting balance.
    #    Customer accounts: from the balance the caller read under its row lock.
    #    If that read was stale (a missing lock), the postings_apply trigger
    #    sees a different real balance and refuses the write (LL004).
    #    System accounts: read now; only the chain-head holder can post to them.
    postings: list[dict] = []
    running: dict[int, int] = {}
    for line_no, line in enumerate(lines, start=1):
        acct = line.account
        if acct["id"] in running:
            base = running[acct["id"]]
        elif acct["kind"] == "customer":
            base = acct["balance"]
        else:
            cur.execute("SELECT balance FROM accounts WHERE id = %s", (acct["id"],))
            base = cur.fetchone()["balance"]
        after = base + line.amount
        running[acct["id"]] = after
        postings.append({"line_no": line_no, "account_id": acct["id"], "amount": line.amount,
                         "balance_after": after})

    observed = None
    if status == "refused" and observe_account_id is not None:
        # Read after the chain-head lock: consistent with every earlier seq.
        cur.execute("SELECT balance FROM accounts WHERE id = %s", (observe_account_id,))
        found = cur.fetchone()
        observed = found["balance"] if found else None

    row = {
        "seq": seq,
        "entry_id": uuid.uuid4(),
        "created_at": created_at,
        "kind": kind,
        "status": status,
        "actor_id": actor_id,
        "account_id": account_id,
        "counterparty_id": counterparty_id,
        "amount": amount,
        "currency": "INR" if amount is not None else None,
        "idempotency_key": idempotency_key,
        "reason_code": reason_code,
        "memo": memo or None,
        "ref_seq": ref_seq,
        "observed_balance": observed,
        "details": details or {},
        "prev_hash": head["last_hash"],
    }

    # 3) Hash (commits to the previous hash => the whole history) and sign.
    signer = get_signer()
    entry_hash = body_hash(entry_body(row, postings))
    signature = signer.sign(entry_hash)

    cur.execute(
        """INSERT INTO journal (seq, entry_id, created_at, kind, status, actor_id, account_id,
                                counterparty_id, amount, currency, idempotency_key, reason_code,
                                memo, ref_seq, observed_balance, details, prev_hash, hash,
                                key_id, signature, txid)
           VALUES (%(seq)s, %(entry_id)s, %(created_at)s, %(kind)s, %(status)s, %(actor_id)s,
                   %(account_id)s, %(counterparty_id)s, %(amount)s, %(currency)s,
                   %(idempotency_key)s, %(reason_code)s, %(memo)s, %(ref_seq)s,
                   %(observed_balance)s, %(details)s, %(prev_hash)s, %(hash)s, %(key_id)s,
                   %(signature)s, 0)""",
        {**row, "details": Jsonb(row["details"]), "hash": entry_hash, "key_id": signer.key_id,
         "signature": signature},
    )

    # 4) The double-entry lines. The trigger applies each one to the account
    #    balance; the balance_floor CHECK is evaluated right there.
    for index, posting in enumerate(postings):
        if index == 1:
            # Between the debit and the credit: tests use this to prove that a
            # request failing halfway leaves no trace (the transaction rolls back).
            hooks.pause_point("between_postings", cur.connection)
            hooks.fault_point("between_postings")
        cur.execute(
            "INSERT INTO postings (entry_seq, line_no, account_id, amount, balance_after) "
            "VALUES (%s, %s, %s, %s, %s)",
            (seq, posting["line_no"], posting["account_id"], posting["amount"], posting["balance_after"]),
        )

    # 5) Advance the chain head (the head guard checks the link).
    cur.execute("UPDATE ledger_head SET last_seq = %s, last_hash = %s, last_at = %s WHERE id = 1",
                (seq, entry_hash, created_at))

    return {**row, "hash": entry_hash, "signature": signature, "key_id": signer.key_id,
            "postings": postings}
