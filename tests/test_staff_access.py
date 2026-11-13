"""Who can become staff: an auditor or an administrator.

Public sign-up makes account holders and nothing else. An auditor or an
administrator exists only because an administrator created them, on the
record, and they can sign in straight away: there is no request to approve.
The database refuses every shortcut: staff made off the record or by the wrong
person, or a role changed after creation.

Until 13 Nov 2026 sign-up also let people ask for auditor access, which an
administrator then approved or declined. Those requests and decisions stay in
the database as history, and that history cannot be rewritten (LL012)."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from app import db
from app.ledger import admin_ops
from app.ledger.journal import append_entry
from tests.helpers import PASSWORD, admin, entries, http_login, key, make_user, open_account, run_proof, unique


def sign_in(server: str, username: str, password: str = PASSWORD) -> httpx.Response:
    return httpx.post(f"{server}/api/auth/login", json={"username": username, "password": password}, timeout=30)


def users_named(username: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM users WHERE username = %s", (username,)).fetchone()["n"]


def requests_named(username: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM access_requests WHERE username = %s",
                            (username,)).fetchone()["n"]


def old_request(username: str | None = None) -> dict:
    """An auditor request as sign-up stored them before 13 Nov 2026 (it no longer can)."""
    with db.connect() as conn:
        return conn.execute("INSERT INTO access_requests (username, display_name, role, password_hash) "
                            "VALUES (%s, 'Asked Before', 'auditor', 'x') RETURNING *",
                            (username or unique("old"),)).fetchone()


def decide_in_sql(cur, request: dict, actor_id: int, decision: str, note: str) -> None:
    """A decision exactly as the old approve/decline endpoints wrote it: a signed
    ACCESS_DECISION entry by the decider, then the request row pointing at it."""
    entry = append_entry(cur, kind="ACCESS_DECISION", status="info", actor_id=actor_id, memo=note,
                         details={"request_id": request["id"], "username": request["username"],
                                  "role": "auditor", "decision": decision})
    cur.execute("UPDATE access_requests SET status = %s, decided_by = %s, decided_at = clock_timestamp(), "
                "decision_note = %s, decision_seq = %s WHERE id = %s",
                (decision, actor_id, note, entry["seq"], request["id"]))


def request_row(request_id: int) -> dict:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM access_requests WHERE id = %s", (request_id,)).fetchone()


# ------------------------------------------------------------------ how staff are made now

def test_an_administrator_creates_an_auditor_who_can_sign_in_at_once(server):
    name = unique("aud")
    r = httpx.post(f"{server}/api/users", headers=http_login(server, admin().username), timeout=30,
                   json={"username": name, "display_name": "New Auditor", "role": "auditor", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["role"] == "auditor", r.text

    # No request, no waiting: the auditor signs in now, reads everything, moves nothing.
    r = sign_in(server, name)
    assert r.status_code == 200 and r.json()["user"]["role"] == "auditor", r.text
    auth = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert httpx.get(f"{server}/api/audit/proof", headers=auth, timeout=60).status_code == 200
    target = open_account(make_user(), 0)
    r = httpx.post(f"{server}/api/deposits", headers={**auth, "Idempotency-Key": key()},
                   json={"account": target, "amount": "100"})
    assert r.status_code == 403
    assert requests_named(name) == 0

    # On the record, by the administrator who made them.
    created = entries("kind = 'USER_CREATED' AND details->>'username' = %s", (name,))
    assert len(created) == 1 and created[0]["actor_id"] == admin().id
    assert created[0]["details"]["role"] == "auditor"


def test_only_an_administrator_creates_staff(server):
    body = {"display_name": "Would-be Auditor", "role": "auditor", "password": PASSWORD}
    for auth, expected in ((http_login(server, make_user("auditor").username), 403),
                           (http_login(server, make_user().username), 403), ({}, 401)):
        name = unique("aud")
        r = httpx.post(f"{server}/api/users", headers=auth, json={**body, "username": name}, timeout=30)
        assert r.status_code == expected, r.text
        assert users_named(name) == 0


def test_sign_up_cannot_make_an_auditor(server):
    name = unique("spy")
    r = httpx.post(f"{server}/api/auth/register", timeout=30,
                   json={"username": name, "display_name": "Would-be Auditor", "password": PASSWORD,
                         "role": "auditor"})
    assert r.status_code == 422, r.text
    assert "administrator creates auditor" in r.text
    # Nothing was made: no user, no request, nothing to sign in with.
    assert users_named(name) == 0 and requests_named(name) == 0
    r = sign_in(server, name)
    assert r.status_code == 401 and r.json()["error"]["code"] == "BAD_CREDENTIALS"


# ------------------------------------------------------------------ the database refuses shortcuts

def test_a_user_keeps_the_role_it_was_created_with():
    holder = make_user()
    with db.connect() as conn:
        for sql in ("UPDATE users SET role = 'admin' WHERE id = %s",
                    "UPDATE users SET role = 'auditor' WHERE id = %s",
                    "UPDATE users SET username = 'renamed' || id WHERE id = %s"):
            with pytest.raises(psycopg.Error) as err:
                conn.execute(sql, (holder.id,))
            assert err.value.sqlstate == "LL013", sql
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (holder.id,))  # still fine


def test_every_user_is_created_on_the_record_and_staff_only_by_an_admin():
    holder = make_user()
    insert = ("INSERT INTO users (username, display_name, role, password_hash) VALUES (%s, 'Off The Record', %s, 'x') "
              "RETURNING id")
    attempts = [
        ("auditor", None, None),            # no USER_CREATED entry at all
        ("auditor", "holder", "auditor"),   # on the record, but made by an account holder
        ("auditor", "self", "auditor"),     # an auditor who made themselves
        ("admin", "self", "admin"),         # an administrator who made themselves
        ("admin", "admin", "holder"),       # recorded as a holder, stored as an admin
    ]
    with db.connection() as conn:
        cur = conn.cursor()
        for role, creator, recorded_role in attempts:
            with pytest.raises(psycopg.Error) as err:
                with conn.transaction():
                    cur.execute(insert, (unique("ghost"), role))
                    new_id = cur.fetchone()["id"]
                    if creator:
                        actor = {"holder": holder.id, "self": new_id, "admin": admin().id}[creator]
                        append_entry(cur, kind="USER_CREATED", status="info", actor_id=actor,
                                     details={"user_id": new_id, "role": recorded_role})
            assert err.value.sqlstate == "LL014", (role, creator, recorded_role)


def test_the_proof_catches_a_role_changed_behind_the_guards():
    holder = make_user()
    with db.connect() as conn:
        conn.execute("ALTER TABLE users DISABLE TRIGGER users_guard")      # what a database owner could do
        try:
            conn.execute("UPDATE users SET role = 'admin' WHERE id = %s", (holder.id,))
            access = next(c for c in run_proof()["checks"] if c["id"] == "access")
            assert not access["ok"] and any(holder.username in p for p in access["problems"]), access
        finally:
            conn.execute("UPDATE users SET role = 'holder' WHERE id = %s", (holder.id,))
            conn.execute("ALTER TABLE users ENABLE TRIGGER users_guard")
    assert next(c for c in run_proof()["checks"] if c["id"] == "access")["ok"]


# ------------------------------------------------------------------ requests from before 13 Nov 2026

def test_old_auditor_requests_cannot_be_rewritten():
    with db.connect() as conn:                                           # what could never be asked for
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO access_requests (username, display_name, role, password_hash) "
                         "VALUES (%s, 'Wants Admin', 'admin', 'x')", (unique("adm"),))
        with pytest.raises(psycopg.Error) as err:
            conn.execute("INSERT INTO access_requests (username, display_name, role, password_hash, status) "
                         "VALUES (%s, 'Pre-approved', 'auditor', 'x', 'declined')", (unique("aud"),))
        assert err.value.sqlstate == "LL012"

    req, auditor = old_request(), make_user("auditor")
    with db.connection() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.Error) as err:                       # a decision with no journal entry
            with conn.transaction():
                cur.execute("UPDATE access_requests SET status = 'declined', decided_by = %s, decided_at = now(), "
                            "decision_seq = (SELECT max(seq) FROM journal) WHERE id = %s", (admin().id, req["id"]))
        assert err.value.sqlstate == "LL012"
        with pytest.raises(psycopg.Error) as err:                       # on the record, but by an auditor
            with conn.transaction():
                decide_in_sql(cur, req, auditor.id, "declined", "not mine to decide")
        assert err.value.sqlstate == "LL012"
        with conn.transaction():                                        # the way it was decided: by an admin
            decide_in_sql(cur, req, admin().id, "declined", "first answer")
        with pytest.raises(psycopg.Error) as err:                       # and decided once
            with conn.transaction():
                decide_in_sql(cur, req, admin().id, "declined", "second answer")
        assert err.value.sqlstate == "LL012"
    assert request_row(req["id"])["decision_note"] == "first answer"
    assert next(c for c in run_proof()["checks"] if c["id"] == "access")["ok"]


def test_an_old_waiting_request_does_not_block_its_name(server):
    req = old_request()                                                  # asked, never decided
    created = admin_ops.create_user(admin().id, req["username"], "Asked Before", "auditor", PASSWORD)
    assert created["role"] == "auditor"
    assert sign_in(server, req["username"]).status_code == 200
    assert request_row(req["id"])["status"] == "pending"                 # the old request is left as it was
