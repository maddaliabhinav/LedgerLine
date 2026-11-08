"""Public self-service sign-up: anyone can become an account holder with an
empty account - and nothing more. (Asking for auditor access is a request an
administrator decides: tests/test_access_requests.py.)"""

from __future__ import annotations

import httpx

from app import db
from app.config import get_settings
from tests.helpers import assert_books_balance, balance, entries, key, unique

PASSWORD = "a-long-enough-password"


def sign_up(server: str, **body) -> httpx.Response:
    payload = {"username": unique("new"), "display_name": "New Person", "password": PASSWORD}
    payload.update(body)
    return httpx.post(f"{server}/api/auth/register", json=payload, timeout=30)


def users_named(username: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM users WHERE username = %s", (username,)).fetchone()["n"]


def test_sign_up_gives_an_account_holder_with_one_empty_account(server):
    r = sign_up(server)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["user"]["role"] == "holder"
    acct = body["account"]["account_no"]
    assert balance(acct) == 0
    auth = {"Authorization": "Bearer " + body["access_token"]}
    mine = httpx.get(f"{server}/api/accounts", headers=auth).json()["accounts"]
    assert [a["account_no"] for a in mine] == [acct]
    # A brand-new user still cannot mint money or act as staff.
    r = httpx.post(f"{server}/api/deposits", headers={**auth, "Idempotency-Key": key()},
                   json={"account": acct, "amount": "100000"})
    assert r.status_code == 403
    assert httpx.get(f"{server}/api/users", headers=auth).status_code == 403
    assert httpx.get(f"{server}/api/audit/proof", headers=auth).status_code == 403
    assert balance(acct) == 0
    # On the record, attributed to the new user, marked as self-service.
    created = entries("kind = 'USER_CREATED' AND details->>'username' = %s", (body["user"]["username"],))
    opened = entries("kind = 'ACCOUNT_OPENED' AND details->>'account_no' = %s", (acct,))
    assert created[0]["details"]["via"] == "self-service sign-up" and created[0]["details"]["role"] == "holder"
    assert opened[0]["actor_id"] == created[0]["actor_id"]
    assert_books_balance()


def test_sign_up_cannot_choose_admin_or_a_balance(server):
    for extra in ({"role": "admin"}, {"role": "ADMIN"}, {"role": "superuser"}, {"role": None},
                  {"balance": "100000"}, {"is_active": True}):
        name = unique("sneaky")
        r = sign_up(server, username=name, **extra)
        assert r.status_code == 422, (extra, r.text)
        assert users_named(name) == 0
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) AS n FROM access_requests WHERE username = %s",
                                (name,)).fetchone()["n"] == 0


def test_sign_up_refuses_taken_names_and_weak_input(server):
    name = unique("taken")
    assert sign_up(server, username=name).status_code == 201
    assert sign_up(server, username=name).status_code == 409
    assert sign_up(server, username=name.upper()).status_code == 409          # usernames are case-insensitive
    assert sign_up(server, password="short").status_code == 422
    assert sign_up(server, username="no spaces allowed").status_code == 422
    assert sign_up(server, display_name="bad\u0000name").status_code == 422
    assert users_named(name) == 1


def test_sign_up_is_rate_limited_per_address(server):
    limit = get_settings().signup_max
    for _ in range(limit):
        assert sign_up(server).status_code == 201
    r = sign_up(server)
    assert r.status_code == 429 and r.json()["error"]["code"] == "TOO_MANY_REQUESTS"
