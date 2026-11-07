#!/usr/bin/env python3
"""Red-team LedgerLine over HTTP, the way the judges will.

    python tools/attack.py                                   # local server, demo credentials
    python tools/attack.py --url https://your-app.onrender.com

It opens two fresh sandbox accounts for the holder (so demo balances are not
drained), then fires concurrent overdraws, replay storms, key reuse, theft,
minting, malformed amounts, forged tokens, edit attempts and deadlock bait,
and finally asks the auditor's proof whether the books still balance.
Exit code 0 = every attack was stopped.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'STOPPED' if ok else 'BROKEN '}] {name}: {detail}", flush=True)


class Client:
    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        r = httpx.post(f"{self.url}/api/auth/login", json={"username": user, "password": password}, timeout=60)
        if r.status_code != 200:
            raise SystemExit(f"cannot sign in as {user}: {r.status_code} {r.text}")
        self.headers = {"Authorization": "Bearer " + r.json()["access_token"]}
        self.user = user

    def get(self, path: str) -> httpx.Response:
        return httpx.get(f"{self.url}{path}", headers=self.headers, timeout=120)

    def post(self, path: str, body: dict, key: str | None = "auto") -> httpx.Response:
        headers = dict(self.headers)
        if key:
            headers["Idempotency-Key"] = str(uuid.uuid4()) if key == "auto" else key
        return httpx.post(f"{self.url}{path}", headers=headers, json=body, timeout=120)


def together(fns):
    barrier = threading.Barrier(len(fns))

    def call(fn):
        barrier.wait()
        return fn()

    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        return list(pool.map(call, fns))


def paise(client: Client, account: str) -> int:
    return client.get(f"/api/accounts/{account}").json()["balance"]["paise"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--admin", default="admin:LedgerAdmin#2026")
    p.add_argument("--holder", default="priya:LedgerPriya#2026")
    p.add_argument("--victim", default="rahul:LedgerRahul#2026")
    p.add_argument("--auditor", default="auditor:LedgerAudit#2026")
    args = p.parse_args()

    print(f"Attacking {args.url}\n")
    admin = Client(args.url, *args.admin.split(":", 1))
    holder = Client(args.url, *args.holder.split(":", 1))
    victim = Client(args.url, *args.victim.split(":", 1))
    auditor = Client(args.url, *args.auditor.split(":", 1))

    start_proof = auditor.get("/api/audit/proof").json()
    print(f"proof before: {'all checks pass' if start_proof['ok'] else 'FAILING'} over {start_proof['entries']} entries\n")

    # Sandbox: two fresh accounts for the holder, Rs 1,000 in the first.
    stamp = time.strftime("%H%M%S")
    a = admin.post("/api/accounts", {"owner_username": holder.user, "name": f"Attack sandbox A {stamp}"}, key=None).json()["account_no"]
    b = admin.post("/api/accounts", {"owner_username": holder.user, "name": f"Attack sandbox B {stamp}"}, key=None).json()["account_no"]
    admin.post("/api/deposits", {"account": a, "amount": "1000.00", "memo": "attack sandbox"})
    print(f"sandbox: {a} holds Rs 1,000.00, {b} holds Rs 0.00\n")

    # 1. Two withdrawals at the same instant.
    rs = together([lambda: holder.post("/api/withdrawals", {"account": a, "amount": "600"})] * 2)
    codes = sorted(r.status_code for r in rs)
    record("two Rs 600 withdrawals at once from Rs 1,000", codes == [201, 409] and paise(holder, a) == 40000,
           f"statuses {codes}, balance now Rs {paise(holder, a) / 100:.2f}")

    # 2. Burst of 25 transfers of Rs 100 against Rs 400.
    rs = together([lambda: holder.post("/api/transfers", {"from_account": a, "to_account": b, "amount": "100"})] * 25)
    paid = sum(1 for r in rs if r.status_code == 201)
    bal = paise(holder, a)
    record("25 simultaneous Rs 100 transfers against Rs 400", paid == 4 and bal == 0,
           f"{paid} paid, {sum(1 for r in rs if r.status_code == 409)} refused, balance Rs {bal / 100:.2f}")

    # 3. Replay storm: one key, 20 copies at once.
    k = str(uuid.uuid4())
    before = paise(holder, b)
    rs = together([lambda: holder.post("/api/transfers", {"from_account": b, "to_account": a, "amount": "10"}, key=k)] * 20)
    moved = before - paise(holder, b)
    record("same request x20 with one Idempotency-Key", moved == 1000 and len({r.content for r in rs}) == 1,
           f"moved Rs {moved / 100:.2f} once, {sum(1 for r in rs if r.headers.get('Idempotent-Replayed'))} replays, "
           f"{len({r.content for r in rs})} distinct response body")

    # 4. Same key, different amount.
    r = holder.post("/api/transfers", {"from_account": b, "to_account": a, "amount": "99"}, key=k)
    record("reuse a key for a bigger amount", r.status_code == 409 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED",
           f"{r.status_code} {r.json()['error']['code']}")

    # 5. Steal from another user's account.
    victim_acct = victim.get("/api/accounts").json()["accounts"][0]["account_no"]
    v_before = paise(admin, victim_acct)
    r1 = holder.post("/api/transfers", {"from_account": victim_acct, "to_account": a, "amount": "1"})
    r2 = holder.post("/api/withdrawals", {"account": victim_acct, "amount": "1"})
    r3 = holder.get(f"/api/accounts/{victim_acct}/statement")
    record("move or read another user's money", (r1.status_code, r2.status_code, r3.status_code) == (403, 403, 403)
           and paise(admin, victim_acct) == v_before, f"transfer {r1.status_code}, withdraw {r2.status_code}, statement {r3.status_code}")

    # 6. Mint money.
    r1 = holder.post("/api/deposits", {"account": a, "amount": "100000"})
    r2 = auditor.post("/api/deposits", {"account": a, "amount": "100000"})
    record("deposit as holder / as auditor", (r1.status_code, r2.status_code) == (403, 403), f"{r1.status_code}, {r2.status_code}")

    # 7. Malformed amounts.
    bad = [-500, 0, "0.00", 10.5, "1e5", "١٠٠", True, "1.001", "99999999999", "-1"]
    statuses = [holder.post("/api/transfers", {"from_account": b, "to_account": a, "amount": x}).status_code for x in bad]
    record("negative / zero / float / exponent / unicode / bool / huge amounts", set(statuses) == {422}, f"statuses {sorted(set(statuses))}")

    # 8. Forged tokens.
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    none_token = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': '1', 'role': 'admin', 'tv': 0, 'exp': 9999999999})}."
    tampered = holder.headers["Authorization"].split()[1][:-4] + "AAAA"
    codes = [httpx.get(f"{args.url}/api/accounts", headers={"Authorization": f"Bearer {t}"}, timeout=60).status_code
             for t in (none_token, tampered)]
    record("alg=none admin token / tampered signature", codes == [401, 401], f"statuses {codes}")

    # 9. Edit the ledger through the API.
    seq = rs[0].json()["entry"]["seq"]
    codes = [httpx.request(m, f"{args.url}/api/entries/{seq}", headers=admin.headers, timeout=60).status_code
             for m in ("PUT", "PATCH", "DELETE")]
    record("PUT / PATCH / DELETE a journal entry", codes == [405, 405, 405], f"statuses {codes}")

    # 10. Deadlock bait: opposite transfers at once.
    admin.post("/api/deposits", {"account": b, "amount": "500.00", "memo": "deadlock bait"})
    rs = together([lambda: holder.post("/api/transfers", {"from_account": a, "to_account": b, "amount": "1"}),
                   lambda: holder.post("/api/transfers", {"from_account": b, "to_account": a, "amount": "1"})] * 10)
    record("20 opposite transfers A<->B at once (deadlock bait)", all(r.status_code < 500 for r in rs),
           f"statuses {sorted({r.status_code for r in rs})}")

    # 11. Pay a system account directly.
    r = holder.post("/api/transfers", {"from_account": b, "to_account": "SYS-SUSPENSE", "amount": "1"})
    record("send money straight into a system account", r.status_code == 422, f"{r.status_code}")

    end = auditor.get("/api/audit/proof").json()
    failing = [c["title"] for c in end["checks"] if not c["ok"]]
    record("auditor's proof after the attack", end["ok"] and end["totals"]["sum_of_all_balances"] == 0,
           "all checks pass, sum of balances 0" if end["ok"] else f"failing: {failing}")

    stopped = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{stopped}/{len(RESULTS)} attacks stopped")
    return 0 if stopped == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
