#!/usr/bin/env python3
"""Verify a LedgerLine evidence bundle OFFLINE, trusting nothing but a public key.

This file deliberately does not import the application: it is an independent
re-implementation of the rules, so a bug in the server cannot hide itself here.
It needs only Python 3.10+ and the `cryptography` package.

    python tools/verify_bundle.py ledgerline-evidence.json --public-key <BASE64>

Get the public key independently (README, or GET /api/public-key saved on a
day you trusted the server). Without --public-key the bundle's own key is used,
which proves internal consistency but not authorship.

Checks: hash of every entry, chain links, gapless sequence, Ed25519 signature of
every entry, double-entry structure, running-balance replay, floors (with
overdraft history), sum of balances == 0, suspense == open holds, each hold
decided at most once, each entry reversed at most once, no idempotency key used
twice. Exit code 0 = all good, 1 = problems found.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from collections import defaultdict

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GENESIS_PREV = "0" * 64


def canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False).encode("ascii")


def verify(bundle: dict, public_key_b64: str) -> dict:
    public_bytes = base64.b64decode(public_key_b64)
    key = Ed25519PublicKey.from_public_bytes(public_bytes)
    key_id = hashlib.sha256(public_bytes).hexdigest()[:16]

    problems: list[str] = []
    prev, expected_seq = GENESIS_PREV, 1
    running: dict[int, int] = defaultdict(int)
    floor: dict[int, int] = defaultdict(int)
    allow_negative: dict[int, bool] = {}
    names: dict[int, str] = {}
    suspense_id = None
    open_holds: dict[int, int] = {}
    decided, reversed_, keys = set(), set(), set()

    for item in bundle["entries"]:
        body = item["body"]
        seq = body["seq"]
        digest = hashlib.sha256(canonical(body)).hexdigest()
        if digest != item["hash"]:
            problems.append(f"#{seq}: content does not match its hash")
        if body["prev_hash"] != prev:
            problems.append(f"#{seq}: broken link to the previous entry")
        if seq != expected_seq:
            problems.append(f"#{seq}: expected #{expected_seq} (gap or reordering)")
        if item.get("key_id") != key_id:
            problems.append(f"#{seq}: signed with unknown key {item.get('key_id')}")
        try:
            key.verify(base64.b64decode(item["signature"]), bytes.fromhex(item["hash"]))
        except (InvalidSignature, ValueError):
            problems.append(f"#{seq}: signature does not verify")
        prev, expected_seq = item["hash"], seq + 1

        kind, status, details = body["kind"], body["status"], body["details"]
        if kind == "GENESIS":
            for acct in details.get("system_accounts", []):
                allow_negative[acct["id"]] = acct["allow_negative"]
                names[acct["id"]] = acct["account_no"]
                if acct["account_no"] == "SYS-SUSPENSE":
                    suspense_id = acct["id"]
        elif kind == "ACCOUNT_OPENED":
            allow_negative[details["account_id"]] = False
            names[details["account_id"]] = details["account_no"]
        elif kind == "OVERDRAFT_LIMIT":
            floor[body["account_id"]] = int(details["to"])

        postings = body["postings"]
        total = sum(p["amount"] for p in postings)
        credits = sum(p["amount"] for p in postings if p["amount"] > 0)
        if status == "posted":
            if len(postings) < 2 or total != 0 or credits != body["amount"]:
                problems.append(f"#{seq}: posted entry is not balanced double entry")
        elif postings:
            problems.append(f"#{seq}: a {status} entry moved money")

        for p in postings:
            acct = p["account_id"]
            if acct not in allow_negative:
                problems.append(f"#{seq}: posting to an account that was never opened ({acct})")
            running[acct] += p["amount"]
            if running[acct] != p["balance_after"]:
                problems.append(f"#{seq}: {names.get(acct, acct)} replays to {running[acct]}, entry says {p['balance_after']}")
            if not allow_negative.get(acct, False) and p["balance_after"] < -floor[acct]:
                problems.append(f"#{seq}: {names.get(acct, acct)} went below its floor ({p['balance_after']})")

        if status == "posted":
            if kind == "HOLD":
                open_holds[seq] = body["amount"]
            elif kind in ("HOLD_APPROVED", "HOLD_REJECTED"):
                if body["ref_seq"] in decided:
                    problems.append(f"#{seq}: hold #{body['ref_seq']} decided twice")
                decided.add(body["ref_seq"])
                open_holds.pop(body["ref_seq"], None)
            elif kind == "REVERSAL":
                if body["ref_seq"] in reversed_:
                    problems.append(f"#{seq}: entry #{body['ref_seq']} reversed twice")
                reversed_.add(body["ref_seq"])
        if body.get("idempotency_key"):
            marker = (body["actor_id"], body["idempotency_key"])
            if marker in keys:
                problems.append(f"#{seq}: idempotency key {body['idempotency_key']} used twice")
            keys.add(marker)

    if sum(running.values()) != 0:
        problems.append(f"sum of all balances is {sum(running.values())}, not 0: money was created or destroyed")
    if suspense_id is not None and running[suspense_id] != sum(open_holds.values()):
        problems.append("suspense balance does not equal the payments still under review")

    return {
        "ok": not problems,
        "entries": len(bundle["entries"]),
        "head_hash": prev,
        "key_id": key_id,
        "balances": {names.get(a, str(a)): b for a, b in sorted(running.items())},
        "open_holds": len(open_holds),
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle")
    parser.add_argument("--public-key", help="base64 Ed25519 public key obtained independently")
    args = parser.parse_args(argv)
    with open(args.bundle, encoding="utf-8") as fh:
        bundle = json.load(fh)
    if not args.public_key:
        print("WARNING: no --public-key given; using the key inside the bundle (proves consistency, not authorship)")
    result = verify(bundle, args.public_key or bundle["public_key"])
    print(f"entries: {result['entries']}   key: {result['key_id']}   head: {result['head_hash'][:16]}...")
    for name, balance in result["balances"].items():
        sign = "-" if balance < 0 else ""
        print(f"  {name:14s} {sign}Rs {abs(balance) // 100:,}.{abs(balance) % 100:02d}")
    if result["ok"]:
        print("VERIFIED: chain, signatures, double entry, floors and conservation all hold.")
        return 0
    print(f"FAILED: {len(result['problems'])} problem(s)")
    for problem in result["problems"][:50]:
        print("  -", problem)
    return 1


if __name__ == "__main__":
    sys.exit(main())
