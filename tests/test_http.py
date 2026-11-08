"""End to end over real HTTP (uvicorn in-process): the routes a judge will hit with curl."""

from __future__ import annotations

import base64
import json
import time
import uuid

import httpx
import jwt

from app import hooks
from app.config import get_settings
from app.ledger import admin_ops
from tests.helpers import (RUPEE, PASSWORD, admin, all_at_once, assert_books_balance, backstops, balance, entries,
                           http_login, key, make_user, open_account)


def post(base, path, headers, body, k=None):
    h = dict(headers)
    if k:
        h["Idempotency-Key"] = k
    return httpx.post(f"{base}{path}", headers=h, json=body, timeout=30)


def test_two_withdrawals_over_http_cannot_overdraw(server):
    holder = make_user()
    acct = open_account(holder, 1000)
    auth = http_login(server, holder.username)
    before = backstops()
    with hooks.race_window(after_balance_check=0.2):
        results = all_at_once([lambda: post(server, "/api/withdrawals", auth,
                                            {"account": acct, "amount": "600.00"}, key())] * 10)
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201] + [409] * 9, statuses
    assert {r.json()["error"]["code"] for r in results if r.status_code == 409} == {"INSUFFICIENT_FUNDS"}
    assert balance(acct) == 400 * RUPEE
    assert backstops() == before
    assert_books_balance()


def test_replayed_request_over_http_returns_identical_bytes(server):
    a, b = make_user(), make_user()
    src, dst = open_account(a, 1000), open_account(b, 0)
    auth = http_login(server, a.username)
    k = key()
    with hooks.race_window(after_idempotency_claim=0.2):
        results = all_at_once([lambda: post(server, "/api/transfers", auth,
                                            {"from_account": src, "to_account": dst, "amount": "125.50"}, k)] * 15)
    assert {r.status_code for r in results} == {201}
    assert len({r.content for r in results}) == 1                       # byte-identical answers
    assert sum(1 for r in results if r.headers.get("Idempotent-Replayed") == "true") == 14
    assert balance(dst) == 12550
    assert len(entries("idempotency_key = %s", (k,))) == 1


def test_moving_someone_elses_money_over_http_is_refused(server):
    victim, thief = make_user(), make_user()
    target, loot = open_account(victim, 1000), open_account(thief, 0)
    auth = http_login(server, thief.username)
    r = post(server, "/api/transfers", auth, {"from_account": target, "to_account": loot, "amount": "1000"}, key())
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_ACCOUNT_OWNER"
    r = post(server, "/api/withdrawals", auth, {"account": target, "amount": "1"}, key())
    assert r.status_code == 403
    r = httpx.get(f"{server}/api/accounts/{target}/statement", headers=auth)
    assert r.status_code == 403
    r = httpx.get(f"{server}/api/accounts/{target}", headers=auth)
    assert r.status_code == 403
    assert balance(target) == 1000 * RUPEE and balance(loot) == 0


def test_money_moving_requests_require_an_idempotency_key(server):
    holder = make_user()
    acct = open_account(holder, 100)
    auth = http_login(server, holder.username)
    r = post(server, "/api/withdrawals", auth, {"account": acct, "amount": "1"})
    assert r.status_code == 428 and r.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    r = post(server, "/api/withdrawals", auth, {"account": acct, "amount": "1"}, "short")
    assert r.status_code == 422
    assert balance(acct) == 100 * RUPEE


def test_bad_amounts_are_rejected_before_any_money_logic(server):
    holder = make_user()
    acct, other = open_account(holder, 1000), open_account(make_user(), 0)
    auth = http_login(server, holder.username)
    for bad in (-5, 0, "0", "0.00", "-1", 10.5, True, "1e3", "1,000", " 10", "١٢٣", "1.001", "+5",
                "10000000.01", 99999999999, None, [], {"value": 1}):
        r = post(server, "/api/transfers", auth, {"from_account": acct, "to_account": other, "amount": bad}, key())
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert balance(acct) == 1000 * RUPEE and balance(other) == 0


def test_unknown_fields_are_rejected_not_applied(server):
    auth_admin = http_login(server, admin().username)
    holder = make_user()
    r = httpx.post(f"{server}/api/accounts", headers=auth_admin,
                   json={"owner_username": holder.username, "name": "Rich", "balance": "1000000"})
    assert r.status_code == 422
    r = httpx.post(f"{server}/api/accounts", headers=auth_admin, json={"owner_username": holder.username, "name": "Fine"})
    assert r.status_code == 201 and r.json()["balance"]["paise"] == 0
    auth = http_login(server, holder.username)
    r = post(server, "/api/transfers", auth, {"from_account": r.json()["account_no"], "to_account": "LL-1001",
                                              "amount": "1", "role": "admin"}, key())
    assert r.status_code == 422


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_forged_and_stale_tokens_are_rejected(server):
    holder = make_user()
    acct = open_account(holder, 100)
    now = int(time.time())
    claims = {"sub": str(holder.id), "tv": 0, "role": "admin", "iat": now, "exp": now + 600, "iss": "ledgerline"}
    tokens = {
        "alg none": f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(claims)}.",
        "wrong key": jwt.encode(claims, "not-the-server-secret-but-long-enough-0123456789", algorithm="HS256"),
        "expired": jwt.encode({**claims, "iat": now - 7200, "exp": now - 3600}, get_settings().secret_key,
                              algorithm="HS256"),
        "garbage": "abc.def.ghi",
    }
    for name, token in tokens.items():
        r = httpx.get(f"{server}/api/accounts", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401, name
    r = httpx.get(f"{server}/api/accounts")
    assert r.status_code == 401


def test_role_claim_in_the_token_is_not_trusted(server):
    """Even a correctly signed token that says role=admin cannot deposit for a
    holder: the server re-reads the role from the database on every request."""
    holder = make_user()
    acct = open_account(holder, 0)
    now = int(time.time())
    token = jwt.encode({"sub": str(holder.id), "tv": 0, "role": "admin", "iat": now, "exp": now + 600,
                        "iss": "ledgerline"}, get_settings().secret_key, algorithm="HS256")
    r = post(server, "/api/deposits", {"Authorization": f"Bearer {token}"}, {"account": acct, "amount": "500"}, key())
    assert r.status_code == 403 and r.json()["error"]["code"] == "ROLE_NOT_PERMITTED"
    assert balance(acct) == 0


def test_deactivated_users_lose_access_immediately(server):
    holder = make_user()
    acct = open_account(holder, 100)
    auth = http_login(server, holder.username)
    assert httpx.get(f"{server}/api/accounts", headers=auth).status_code == 200
    admin_ops.set_user_active(admin().id, holder.username, False)
    assert httpx.get(f"{server}/api/accounts", headers=auth).status_code == 401
    r = post(server, "/api/withdrawals", auth, {"account": acct, "amount": "1"}, key())
    assert r.status_code == 401 and balance(acct) == 100 * RUPEE


def test_password_guessing_is_throttled(server):
    holder = make_user()
    for _ in range(get_settings().login_max_failures):
        r = httpx.post(f"{server}/api/auth/login", json={"username": holder.username, "password": "wrong-guess"})
        assert r.status_code == 401
    r = httpx.post(f"{server}/api/auth/login", json={"username": holder.username, "password": PASSWORD})
    assert r.status_code == 429


def test_the_ledger_has_no_edit_or_delete_routes(server):
    auth = http_login(server, admin().username)
    for method in ("PUT", "PATCH", "DELETE"):
        r = httpx.request(method, f"{server}/api/entries/1", headers=auth)
        assert r.status_code == 405, method


def test_auditor_reads_everything_but_moves_nothing(server):
    auditor = make_user("auditor")
    holder = make_user()
    acct = open_account(holder, 100)
    auth = http_login(server, auditor.username)
    assert httpx.get(f"{server}/api/accounts/{acct}/statement", headers=auth).status_code == 200
    assert httpx.get(f"{server}/api/audit/proof", headers=auth, timeout=60).status_code == 200
    r = post(server, "/api/deposits", auth, {"account": acct, "amount": "1"}, key())
    assert r.status_code == 403
    r = httpx.post(f"{server}/api/accounts/{acct}/freeze", headers=auth, json={"reason": "just looking"})
    assert r.status_code == 403
    assert balance(acct) == 100 * RUPEE


def test_security_headers_are_set(server):
    r = httpx.get(f"{server}/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-cache"
    r = httpx.get(f"{server}/static/app.js")
    assert r.headers["cache-control"] == "no-cache"
    r = httpx.get(f"{server}/api/health")
    assert r.headers["cache-control"] == "no-store"
