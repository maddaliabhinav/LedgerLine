"""Exactly-once for money-moving requests.

Where the key lives: table idempotency_keys, primary key (user_id, idem_key).
Keys are scoped per user, so one user can neither collide with nor read
another user's results.

How it works (all inside the SAME database transaction as the money movement):

  1. INSERT the key row first (ON CONFLICT DO NOTHING).
     - No conflict: we own the key. Do the work, store the response, COMMIT.
       The key and the money movement become visible together or not at all.
     - A committed row exists: this is a duplicate. If the request fingerprint
       matches, return the stored response unchanged; if not, refuse (409).
     - An UNCOMMITTED row exists (the first request is still in flight):
       PostgreSQL makes our INSERT wait for that transaction to finish. If it
       commits we return its answer; if it rolls back we proceed as the first.
       We wait at most lock_timeout, then answer 409 REQUEST_IN_PROGRESS
       without moving anything.
  2. Every duplicate answered is written to idempotency_replays, so the data
     shows exactly which requests were duplicates.

How long a key is remembered: forever. A key that expires can be replayed
to pay twice; storage for a key is a few hundred bytes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from psycopg import Cursor

from ..signing import canonical_json


@dataclass
class Outcome:
    http_status: int
    body: dict
    entry_seq: int | None = None
    replayed: bool = False
    headers: dict = field(default_factory=dict)
    text: str | None = None          # the exact response bytes (set when stored)

    def render(self) -> str:
        if self.text is None:
            self.text = json.dumps(self.body, ensure_ascii=False, separators=(",", ":"))
        return self.text


def fingerprint(endpoint: str, payload: dict) -> str:
    return hashlib.sha256(canonical_json({"endpoint": endpoint, "payload": payload})).hexdigest()


def claim(cur: Cursor, user_id: int, key: str, endpoint: str, fp: str) -> bool:
    cur.execute(
        "INSERT INTO idempotency_keys (user_id, idem_key, endpoint, fingerprint) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (user_id, idem_key) DO NOTHING RETURNING user_id",
        (user_id, key, endpoint, fp),
    )
    return cur.fetchone() is not None


def complete(cur: Cursor, user_id: int, key: str, outcome: Outcome) -> None:
    cur.execute(
        "UPDATE idempotency_keys SET completed_at = clock_timestamp(), http_status = %s, "
        "response = %s, entry_seq = %s WHERE user_id = %s AND idem_key = %s AND completed_at IS NULL",
        (outcome.http_status, outcome.render(), outcome.entry_seq, user_id, key),
    )
    if cur.rowcount != 1:
        raise RuntimeError("idempotency record was not in the expected state")


def record_replay(cur: Cursor, user_id: int, key: str, endpoint: str, outcome: str, fp: str,
                  original_seq: int | None) -> None:
    cur.execute(
        "INSERT INTO idempotency_replays (user_id, idem_key, endpoint, outcome, fingerprint, original_entry_seq) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, key, endpoint, outcome, fp, original_seq),
    )


@dataclass
class Duplicate:
    outcome: Outcome | None          # stored response to replay, or None on mismatch
    mismatch: bool
    original_endpoint: str


def answer_duplicate(cur: Cursor, user_id: int, key: str, endpoint: str, fp: str) -> Duplicate:
    cur.execute(
        "SELECT endpoint, fingerprint, http_status, response, entry_seq, completed_at "
        "FROM idempotency_keys WHERE user_id = %s AND idem_key = %s",
        (user_id, key),
    )
    stored = cur.fetchone()
    if stored is None or stored["completed_at"] is None:
        # Cannot happen: rows are only ever visible completed (same-transaction write).
        raise RuntimeError("idempotency record visible but incomplete")
    if stored["fingerprint"] != fp or stored["endpoint"] != endpoint:
        record_replay(cur, user_id, key, endpoint, "key_reused_with_different_request", fp, stored["entry_seq"])
        return Duplicate(None, True, stored["endpoint"])
    record_replay(cur, user_id, key, endpoint, "replayed", fp, stored["entry_seq"])
    outcome = Outcome(stored["http_status"], json.loads(stored["response"]), stored["entry_seq"], replayed=True,
                      headers={"Idempotent-Replayed": "true"}, text=stored["response"])
    return Duplicate(outcome, False, stored["endpoint"])
