"""Asking for auditor access at sign-up.

A request is not a user: nobody can sign in with it until an administrator
approves it. The decision is made exactly once, it is a signed journal entry,
and the database refuses every shortcut: asking for admin, deciding without
being an admin, deciding twice, changing a role, or creating staff off the
record."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from app import db, hooks
from app.errors import Conflict
from app.ledger import admin_ops
from app.ledger.journal import append_entry
from tests.helpers import admin, all_at_once, entries, http_login, key, make_user, open_account, run_proof, unique

PASSWORD = "auditor-password-123"


def ask(server: str, **body) -> httpx.Response:
    payload = {"username": unique("aud"), "display_name": "Asks For Access", "password": PASSWORD,
               "role": "auditor"}
    payload.update(body)
    return httpx.post(f"{server}/api/auth/register", json=payload, timeout=30)


def sign_in(server: str, username: str, password: str = PASSWORD) -> httpx.Response:
    return httpx.post(f"{server}/api/auth/login", json={"username": username, "password": password}, timeout=30)


def login_as(server: str, role: str) -> dict:
    return http_login(server, make_user(role).username)


def decide(server: str, auth: dict, request_id: int, verb: str, note: str | None = None) -> httpx.Response:
    return httpx.post(f"{server}/api/access-requests/{request_id}/{verb}", headers=auth, json={"note": note},
                      timeout=30)


def users_named(username: str) -> int:
    with db.connect() as conn:
        return conn.execute("SELECT count(*) AS n FROM users WHERE username = %s", (username,)).fetchone()["n"]


def request_row(request_id: int) -> dict:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM access_requests WHERE id = %s", (request_id,)).fetchone()


def decisions_for(request_id: int) -> list[dict]:
    return entries("kind = 'ACCESS_DECISION' AND details->>'request_id' = %s", (str(request_id),))


def a_waiting_request() -> dict:
    return admin_ops.request_auditor_access(unique("aud"), "Waiting Person", PASSWORD)


# ------------------------------------------------------------------ the flow

def test_asking_for_auditor_access_creates_a_request_not_a_user(server):
    r = ask(server)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "pending" and "access_token" not in body
    req = body["request"]
    assert req["role"] == "auditor" and req["status"] == "pending"
    assert users_named(req["username"]) == 0
    # The right password is told to wait; a wrong one learns nothing about the request.
    r = sign_in(server, req["username"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "APPROVAL_PENDING", r.text
    r = sign_in(server, req["username"], "not-the-password")
    assert r.status_code == 401 and r.json()["error"]["code"] == "BAD_CREDENTIALS"
    # Asking is not an event in the books; only the decision will be.
    assert decisions_for(req["id"]) == []


def test_an_approved_request_becomes_an_auditor_on_the_record(server):
    req = ask(server).json()["request"]
    r = decide(server, login_as(server, "admin"), req["id"], "approve", "checked with the audit team")
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "approved" and r.json()["user"]["role"] == "auditor"

    # Now, and only now, the person can sign in: as a read-only auditor.
    r = sign_in(server, req["username"])
    assert r.status_code == 200 and r.json()["user"]["role"] == "auditor", r.text
    auth = {"Authorization": "Bearer " + r.json()["access_token"]}
    assert httpx.get(f"{server}/api/audit/proof", headers=auth, timeout=60).status_code == 200
    target = open_account(make_user(), 0)
    r = httpx.post(f"{server}/api/deposits", headers={**auth, "Idempotency-Key": key()},
                   json={"account": target, "amount": "100"})
    assert r.status_code == 403
    assert decide(server, auth, a_waiting_request()["id"], "approve").status_code == 403

    # The signed record: the administrator's decision, then the user, pointing back at it.
    decision = decisions_for(req["id"])
    created = entries("kind = 'USER_CREATED' AND details->>'username' = %s", (req["username"],))
    assert len(decision) == 1 and len(created) == 1
    assert decision[0]["details"]["decision"] == "approved" and decision[0]["memo"] == "checked with the audit team"
    assert created[0]["ref_seq"] == decision[0]["seq"] and created[0]["actor_id"] == decision[0]["actor_id"]
    row = request_row(req["id"])
    assert row["decision_seq"] == decision[0]["seq"] and row["user_id"] is not None
    assert row["password_hash"] is None                      # moved into users, not kept twice


def test_only_an_administrator_can_decide(server):
    req = ask(server).json()["request"]
    for auth, expected in ((login_as(server, "auditor"), 403), (login_as(server, "holder"), 403), ({}, 401)):
        for verb in ("approve", "decline"):
            r = decide(server, auth, req["id"], verb)
            assert r.status_code == expected, (verb, expected, r.text)
    assert request_row(req["id"])["status"] == "pending" and users_named(req["username"]) == 0
    assert decisions_for(req["id"]) == []
    # Admins and auditors can see the queue (never a password hash); holders cannot.
    for role in ("admin", "auditor"):
        r = httpx.get(f"{server}/api/access-requests", headers=login_as(server, role))
        mine = [x for x in r.json()["requests"] if x["id"] == req["id"]]
        assert r.status_code == 200 and mine and mine[0]["status"] == "pending"
        assert "password_hash" not in mine[0]
    assert httpx.get(f"{server}/api/access-requests", headers=login_as(server, "holder")).status_code == 403


def test_a_request_is_decided_exactly_once(server):
    auth = login_as(server, "admin")
    approved, declined = ask(server).json()["request"], ask(server).json()["request"]
    assert decide(server, auth, approved["id"], "approve").status_code == 200
    assert decide(server, auth, declined["id"], "decline").status_code == 200
    for req in (approved, declined):
        for verb in ("approve", "decline"):
            r = decide(server, auth, req["id"], verb)
            assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_DECIDED", (req, verb, r.text)
        assert len(decisions_for(req["id"])) == 1
    assert users_named(approved["username"]) == 1 and users_named(declined["username"]) == 0
    assert decide(server, auth, 987654321, "approve").status_code == 404


def test_a_declined_request_never_signs_in_and_may_ask_again(server):
    req = ask(server).json()["request"]
    r = decide(server, login_as(server, "admin"), req["id"], "decline", "not on the audit team")
    assert r.status_code == 200 and r.json()["user"] is None
    r = sign_in(server, req["username"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCESS_DECLINED", r.text
    assert users_named(req["username"]) == 0
    assert ask(server, username=req["username"]).status_code == 202          # a new request, a new decision
    assert sign_in(server, req["username"]).json()["error"]["code"] == "APPROVAL_PENDING"


def test_a_waiting_request_reserves_its_username(server):
    name = ask(server).json()["request"]["username"]
    r = httpx.post(f"{server}/api/auth/register", timeout=30,
                   json={"username": name, "display_name": "Name Squatter", "password": PASSWORD})
    assert r.status_code == 409, r.text
    assert ask(server, username=name).status_code == 409
    with pytest.raises(Conflict):
        admin_ops.create_user(admin().id, name, "Name Squatter", "holder", PASSWORD)
    assert users_named(name) == 0
    assert ask(server, username=make_user().username).status_code == 409    # and a user's name cannot be asked for


def test_two_admins_deciding_at_once_make_exactly_one_decision():
    for verbs in ((True, True), (True, False)):
        req = a_waiting_request()
        admins = (make_user("admin"), make_user("admin"))
        with hooks.race_window(after_request_check=0.3):
            results = all_at_once([lambda a=a, ok=ok: admin_ops.decide_access_request(a.id, req["id"], ok, None)
                                   for a, ok in zip(admins, verbs)])
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(losers) == 1, results
        assert isinstance(losers[0], Conflict) and losers[0].code == "ALREADY_DECIDED", losers
        assert len(decisions_for(req["id"])) == 1
        status = request_row(req["id"])["status"]
        assert users_named(req["username"]) == (1 if status == "approved" else 0)


# ------------------------------------------------------------------ the database refuses shortcuts

def test_the_database_refuses_requests_for_admin_access():
    with db.connect() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("INSERT INTO access_requests (username, display_name, role, password_hash) "
                         "VALUES (%s, 'Wants Admin', 'admin', 'x')", (unique("adm"),))
        with pytest.raises(psycopg.Error) as err:
            conn.execute("INSERT INTO access_requests (username, display_name, role, password_hash, status) "
                         "VALUES (%s, 'Pre-approved', 'auditor', 'x', 'declined')", (unique("aud"),))
        assert err.value.sqlstate == "LL012"


def test_a_decision_must_be_on_the_record_and_by_an_administrator():
    req = a_waiting_request()
    auditor = make_user("auditor")
    with db.connection() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.Error) as err:                           # no journal entry at all
            with conn.transaction():
                cur.execute("UPDATE access_requests SET status = 'declined', decided_by = %s, decided_at = now(), "
                            "decision_seq = (SELECT max(seq) FROM journal) WHERE id = %s", (admin().id, req["id"]))
        assert err.value.sqlstate == "LL012"
        with pytest.raises(psycopg.Error) as err:                           # on the record, but by an auditor
            with conn.transaction():
                entry = append_entry(cur, kind="ACCESS_DECISION", status="info", actor_id=auditor.id,
                                     details={"request_id": req["id"], "decision": "declined"})
                cur.execute("UPDATE access_requests SET status = 'declined', decided_by = %s, decided_at = now(), "
                            "decision_seq = %s WHERE id = %s", (auditor.id, entry["seq"], req["id"]))
        assert err.value.sqlstate == "LL012"
    assert request_row(req["id"])["status"] == "pending"


def test_a_decided_request_stays_decided_even_in_sql():
    req = a_waiting_request()
    admin_ops.decide_access_request(admin().id, req["id"], False, "first answer")
    with db.connection() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.Error) as err:
            with conn.transaction():
                # A valid-looking second decision, by an administrator, on the record.
                entry = append_entry(cur, kind="ACCESS_DECISION", status="info", actor_id=admin().id,
                                     details={"request_id": req["id"], "decision": "declined"})
                cur.execute("UPDATE access_requests SET decision_seq = %s, decision_note = 'second answer' "
                            "WHERE id = %s", (entry["seq"], req["id"]))
        assert err.value.sqlstate == "LL012"
    assert request_row(req["id"])["decision_note"] == "first answer"


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
