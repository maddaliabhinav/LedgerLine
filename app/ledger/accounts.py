"""Account lookups and row locking.

Locking rule (prevents deadlocks): a transaction locks every CUSTOMER account it
will touch, one row at a time, in ascending id order, BEFORE it takes the
chain-head lock. System accounts (SYS-EXTERNAL, SYS-SUSPENSE) are never locked
by application code: they are only written by postings, and postings are only
written by whoever holds the chain-head lock, so there is never contention on
them.
"""

from __future__ import annotations

import re

from psycopg import Cursor

from .. import hooks

ACCOUNT_COLUMNS = ("id, account_no, owner_id, name, kind, currency, balance, overdraft_limit, "
                   "daily_limit, allow_negative, status, created_at")
CUSTOMER_NO_RE = re.compile(r"LL-[0-9]{4,8}", re.ASCII)


def normalise_account_no(value: str) -> str:
    return (value or "").strip().upper()


def is_customer_no(value: str) -> bool:
    return bool(CUSTOMER_NO_RE.fullmatch(value or ""))


def find(cur: Cursor, account_no: str) -> dict | None:
    cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE account_no = %s", (account_no,))
    return cur.fetchone()


def get(cur: Cursor, account_id: int) -> dict | None:
    cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = %s", (account_id,))
    return cur.fetchone()


def system(cur: Cursor, account_no: str) -> dict:
    row = find(cur, account_no)
    if row is None or row["kind"] != "system":
        raise RuntimeError(f"system account {account_no} is missing; run the migration")
    return row


def lock(cur: Cursor, account_ids) -> dict[int, dict]:
    """Lock customer accounts FOR UPDATE in ascending id order; return fresh rows.

    The balance and status we read here cannot change until we commit, because
    every other writer must take the same row lock first.
    """
    locked: dict[int, dict] = {}
    for account_id in sorted(set(account_ids)):
        cur.execute(f"SELECT {ACCOUNT_COLUMNS} FROM accounts WHERE id = %s AND kind = 'customer' FOR UPDATE",
                    (account_id,))
        row = cur.fetchone()
        if row is None:
            raise RuntimeError(f"account {account_id} vanished or is not a customer account")
        locked[account_id] = row
        hooks.pause_point("between_account_locks", cur.connection)
    return locked


def available(account: dict) -> int:
    """What the account may spend: balance plus any authorised overdraft."""
    return account["balance"] + account["overdraft_limit"]
