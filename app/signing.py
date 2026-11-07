"""Tamper evidence: canonical hashing of journal entries and Ed25519 signatures.

Each journal entry is hashed together with the previous entry's hash, so the
hash of entry N commits to the whole history 1..N. The application signs each
entry hash with a private key that lives in the app's environment, never in
the database. Consequences:

* Editing any row breaks its hash and every later prev_hash link.
* Someone with database access who edits a row AND recomputes every later hash
  still cannot produce valid signatures without the private key.
* Anyone holding the public key can verify an exported ledger offline
  (tools/verify_bundle.py), without trusting this server.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from functools import lru_cache

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .config import get_settings

GENESIS_PREV_HASH = "0" * 64
BODY_VERSION = 1


def canonical_json(obj) -> bytes:
    """Deterministic JSON: sorted keys, no whitespace, ASCII only, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def entry_body(row: dict, postings: list[dict]) -> dict:
    """The exact structure that is hashed, built from journal + postings columns."""
    return {
        "v": BODY_VERSION,
        "seq": row["seq"],
        "entry_id": str(row["entry_id"]),
        "created_at": iso_utc(row["created_at"]),
        "kind": row["kind"],
        "status": row["status"],
        "actor_id": row["actor_id"],
        "account_id": row["account_id"],
        "counterparty_id": row["counterparty_id"],
        "amount": row["amount"],
        "currency": row["currency"],
        "idempotency_key": row["idempotency_key"],
        "reason_code": row["reason_code"],
        "memo": row["memo"],
        "ref_seq": row["ref_seq"],
        "observed_balance": row["observed_balance"],
        "details": row["details"],
        "postings": [
            {"line": p["line_no"], "account_id": p["account_id"], "amount": p["amount"],
             "balance_after": p["balance_after"]}
            for p in sorted(postings, key=lambda p: p["line_no"])
        ],
        "prev_hash": row["prev_hash"],
    }


def body_hash(body: dict) -> str:
    return hashlib.sha256(canonical_json(body)).hexdigest()


class Signer:
    def __init__(self, seed: bytes):
        self._private = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_bytes = self._private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(self.public_bytes).hexdigest()[:16]

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self.public_bytes).decode("ascii")

    def sign(self, hash_hex: str) -> str:
        return base64.b64encode(self._private.sign(bytes.fromhex(hash_hex))).decode("ascii")


def verify_signature(public_bytes: bytes, hash_hex: str, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public_bytes).verify(
            base64.b64decode(signature_b64, validate=True), bytes.fromhex(hash_hex))
        return True
    except (InvalidSignature, ValueError):
        return False


@lru_cache(maxsize=1)
def get_signer() -> Signer:
    settings = get_settings()
    material = settings.signing_key or settings.secret_key
    seed = hashlib.sha256(b"ledgerline/ed25519/v1\x00" + material.encode("utf-8")).digest()
    return Signer(seed)
