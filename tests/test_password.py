"""Changing your own password: current password, new password, the new one again.

Only the person changes their password, and only by knowing the current one.
Wrong guesses are throttled like sign-in. Every other session ends at once, so a
stolen session dies with the old password. The change is on the record (who and
when, never the password or its hash), and the database refuses a password
change that is not (LL018)."""

from __future__ import annotations

import json

import httpx
import psycopg
import pytest

from app import db
from app.config import get_settings
from app.ledger.journal import append_entry
from app.security import hash_password
from tests.helpers import PASSWORD, entries, http_login, make_user

NEW = "a-brand-new-passphrase"


def change(server: str, auth: dict, current: str = PASSWORD, new: str = NEW, again: str | None = None,
           **extra) -> httpx.Response:
    body = {"current_password": current, "new_password": new, "confirm_password": new if again is None else again}
    return httpx.post(f"{server}/api/me/password", headers=auth, json={**body, **extra}, timeout=30)


def sign_in(server: str, username: str, password: str) -> int:
    return httpx.post(f"{server}/api/auth/login", json={"username": username, "password": password},
                      timeout=30).status_code


@pytest.mark.parametrize("role", ["holder", "auditor", "admin"])
def test_everyone_can_change_their_own_password(server, role):
    person = make_user(role)
    here = http_login(server, person.username)
    elsewhere = http_login(server, person.username)                  # another device, or a thief's copy

    r = change(server, here)
    assert r.status_code == 200, r.text
    fresh = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert httpx.get(f"{server}/api/me", headers=fresh).status_code == 200             # this device carries on
    for old in (here, elsewhere):                                                      # every other session ends
        r = httpx.get(f"{server}/api/me", headers=old)
        assert r.status_code == 401 and r.json()["error"]["code"] == "SESSION_REVOKED"
    assert sign_in(server, person.username, PASSWORD) == 401
    assert sign_in(server, person.username, NEW) == 200

    record = entries("kind = 'PASSWORD_CHANGED' AND details->>'user_id' = %s", (str(person.id),))
    assert [(e["actor_id"], e["details"]) for e in record] == \
        [(person.id, {"user_id": person.id, "username": person.username})]
    text = json.dumps(record, default=str)
    assert NEW not in text and PASSWORD not in text and "scrypt" not in text      # who and when, never what


def test_a_wrong_current_password_changes_nothing_and_keeps_you_signed_in(server):
    person = make_user()
    auth = http_login(server, person.username)
    r = change(server, auth, current="not-my-password")
    assert r.status_code == 403 and r.json()["error"]["code"] == "WRONG_PASSWORD", r.text
    assert httpx.get(f"{server}/api/me", headers=auth).status_code == 200
    assert sign_in(server, person.username, PASSWORD) == 200
    assert not entries("kind = 'PASSWORD_CHANGED' AND details->>'user_id' = %s", (str(person.id),))


def test_the_new_password_is_checked(server):
    person = make_user()
    auth = http_login(server, person.username)
    for case in ({"again": NEW + "x"},                     # re-entered differently
                 {"new": "short", "again": "short"},       # under 10 characters
                 {"new": PASSWORD},                        # the same as now
                 {"username": "someone-else"}):            # unknown fields are refused, not applied
        r = change(server, auth, **case)
        assert r.status_code == 422, (case, r.status_code, r.text)
    mismatch = change(server, auth, again=NEW + "x").json()["error"]["fields"]
    assert [f["field"] for f in mismatch] == ["confirm_password"]
    assert sign_in(server, person.username, PASSWORD) == 200          # still the old password


def test_guessing_the_current_password_is_throttled(server):
    person = make_user()
    auth = http_login(server, person.username)
    statuses = [change(server, auth, current=f"guess-number-{i}").status_code
                for i in range(get_settings().login_max_failures)]
    assert set(statuses) == {403}
    r = change(server, auth)                                         # even the right one, for a while
    assert r.status_code == 429, r.text
    assert sign_in(server, person.username, PASSWORD) in (200, 429)
    assert not entries("kind = 'PASSWORD_CHANGED' AND details->>'user_id' = %s", (str(person.id),))


def test_nobody_changes_someone_elses_password(server):
    person, staff = make_user(), http_login(server, make_user("admin").username)
    for method in ("POST", "PUT", "PATCH"):
        r = httpx.request(method, f"{server}/api/users/{person.username}/password", headers=staff,
                          json={"new_password": NEW}, timeout=30)
        assert r.status_code in (404, 405), (method, r.status_code)
    assert sign_in(server, person.username, PASSWORD) == 200


def test_the_database_refuses_a_password_change_off_the_record():
    person, other = make_user(), make_user("admin")
    for actor in (None, other):                                      # no entry, or someone else's entry
        with db.connection() as conn:
            cur = conn.cursor()
            with pytest.raises(psycopg.Error) as err:
                with conn.transaction():
                    cur.execute("UPDATE users SET password_hash = %s WHERE id = %s", (hash_password(NEW), person.id))
                    if actor:
                        append_entry(cur, kind="PASSWORD_CHANGED", status="info", actor_id=actor.id,
                                     details={"user_id": person.id, "username": person.username})
            assert err.value.sqlstate == "LL018", actor
