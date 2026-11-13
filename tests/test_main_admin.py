"""The main administrator.

Any administrator does the daily work, so approvals never wait for one person.
Who IS an administrator is decided by the main administrator alone, and nobody
can remove the main administrator. Nothing is ever deleted: removing access
stops sign-in at once and keeps the history. The database enforces the same."""

from __future__ import annotations

import httpx
import psycopg
import pytest

from app import db
from app.errors import Conflict
from app.ledger import admin_ops
from app.ledger.journal import append_entry
from app.ledger.proof import admin_creation_problem, run_proof as proof_in
from tests.helpers import PASSWORD, admin, entries, http_login, key, make_user, open_account, run_proof, unique


def call(server: str, auth: dict, method: str, path: str, body: dict | None = None) -> httpx.Response:
    return httpx.request(method, f"{server}{path}", headers=auth, json=body, timeout=30)


def main_admin(server: str) -> dict:
    return http_login(server, admin().username)


def new_admin(server: str) -> tuple[str, dict]:
    """An administrator created by the main administrator, signed in."""
    name = unique("adm")
    r = call(server, main_admin(server), "POST", "/api/users",
             {"username": name, "display_name": "Second Admin", "role": "admin", "password": PASSWORD})
    assert r.status_code == 201, r.text
    return name, http_login(server, name)


def remove(server: str, auth: dict, username: str) -> httpx.Response:
    return call(server, auth, "POST", f"/api/users/{username}/deactivate")


def restore(server: str, auth: dict, username: str) -> httpx.Response:
    return call(server, auth, "POST", f"/api/users/{username}/activate")


def flags(username: str) -> dict:
    with db.connect() as conn:
        return conn.execute("SELECT is_active, is_main_admin FROM users WHERE username = %s", (username,)).fetchone()


# ------------------------------------------------------------------ who the main administrator is

def test_the_first_administrator_is_the_one_main_administrator(server):
    first = admin()                                                  # the first administrator of this database
    with db.connect() as conn:
        mains = conn.execute("SELECT username FROM users WHERE is_main_admin").fetchall()
    assert [m["username"] for m in mains] == [first.username]
    me = call(server, main_admin(server), "GET", "/api/me").json()
    assert me["main_admin"] is True and any("create administrators" in p for p in me["permissions"])
    _, second = new_admin(server)
    assert call(server, second, "GET", "/api/me").json()["main_admin"] is False
    # The server's own command line cannot make a second main administrator either.
    with pytest.raises(Conflict):
        admin_ops.create_user(None, unique("adm"), "Another First Admin", "admin", PASSWORD)


# ------------------------------------------------------------------ creating administrators

def test_only_the_main_administrator_creates_administrators(server):
    name, second = new_admin(server)
    created = entries("kind = 'USER_CREATED' AND details->>'username' = %s", (name,))
    assert created[0]["actor_id"] == admin().id                      # on the record, by the main administrator
    r = call(server, second, "POST", "/api/users",
             {"username": unique("adm"), "display_name": "Third Admin", "role": "admin", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "MAIN_ADMIN_ONLY", r.text
    for role in ("holder", "auditor"):                               # other users: any administrator
        r = call(server, second, "POST", "/api/users",
                 {"username": unique(role[:3]), "display_name": f"New {role}", "role": role, "password": PASSWORD})
        assert r.status_code == 201, r.text


def test_a_second_administrator_does_the_daily_work(server):
    _, second = new_admin(server)
    target = open_account(make_user(), 0)
    r = call(server, {**second, "Idempotency-Key": key()}, "POST", "/api/deposits",
             {"account": target, "amount": "2500", "memo": "salary"})
    assert r.status_code == 201 and r.json()["status"] == "posted", r.text
    holder = make_user()
    r = call(server, second, "POST", "/api/accounts", {"owner_username": holder.username, "name": "Second account"})
    assert r.status_code == 201, r.text
    r = call(server, second, "POST", "/api/users",
             {"username": unique("aud"), "display_name": "New Auditor", "role": "auditor", "password": PASSWORD})
    assert r.status_code == 201 and r.json()["role"] == "auditor", r.text


# ------------------------------------------------------------------ removing access

def test_nobody_can_remove_the_main_administrator(server):
    _, second = new_admin(server)
    for auth in (second, main_admin(server)):                          # another admin, or the main admin itself
        r = remove(server, auth, admin().username)
        assert r.status_code == 403 and r.json()["error"]["code"] == "MAIN_ADMIN_PROTECTED", r.text
    assert flags(admin().username)["is_active"] is True
    assert call(server, main_admin(server), "GET", "/api/me").status_code == 200


def test_only_the_main_administrator_removes_or_restores_administrators(server):
    a_name, a = new_admin(server)
    b_name, b = new_admin(server)
    r = remove(server, a, b_name)                                    # one admin cannot lock out another
    assert r.status_code == 403 and r.json()["error"]["code"] == "MAIN_ADMIN_ONLY", r.text
    assert flags(b_name)["is_active"] is True

    assert remove(server, main_admin(server), b_name).status_code == 200
    assert call(server, b, "GET", "/api/me").json()["error"]["code"] == "SESSION_REVOKED"   # at once
    r = httpx.post(f"{server}/api/auth/login", json={"username": b_name, "password": PASSWORD}, timeout=30)
    assert r.status_code == 401
    r = restore(server, a, b_name)
    assert r.status_code == 403 and r.json()["error"]["code"] == "MAIN_ADMIN_ONLY", r.text
    assert restore(server, main_admin(server), b_name).status_code == 200
    http_login(server, b_name)                                       # back, history intact
    status = entries("kind = 'USER_STATUS' AND details->>'username' = %s", (b_name,))
    assert [e["details"]["active"] for e in status] == [False, True]
    assert {e["actor_id"] for e in status} == {admin().id}


def test_any_administrator_manages_holders_and_auditors(server):
    _, second = new_admin(server)
    for role in ("holder", "auditor"):
        person = make_user(role)
        assert remove(server, second, person.username).status_code == 200
        assert restore(server, second, person.username).status_code == 200


# ------------------------------------------------------------------ the database refuses shortcuts

def test_the_database_keeps_the_main_administrator():
    other = make_user("admin")
    with db.connect() as conn:
        for sql, params, code in (
            ("UPDATE users SET is_active = false WHERE id = %s", (admin().id,), "LL015"),
            ("UPDATE users SET is_main_admin = false WHERE id = %s", (admin().id,), "LL013"),
            ("UPDATE users SET is_main_admin = true WHERE id = %s", (other.id,), "LL013"),
        ):
            with pytest.raises(psycopg.Error) as err:
                conn.execute(sql, params)
            assert err.value.sqlstate == code, sql
    assert flags(admin().username) == {"is_active": True, "is_main_admin": True}


def test_the_database_allows_one_main_administrator():
    with db.connection() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                cur.execute("INSERT INTO users (username, display_name, role, password_hash, is_main_admin) "
                            "VALUES (%s, 'Second Main', 'admin', 'x', true)", (unique("adm"),))


def test_the_database_takes_administrators_only_from_the_main_administrator():
    second = make_user("admin")
    with db.connection() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.Error) as err:
            with conn.transaction():
                cur.execute("INSERT INTO users (username, display_name, role, password_hash) "
                            "VALUES (%s, 'Off The Record', 'admin', 'x') RETURNING id", (unique("adm"),))
                new_id = cur.fetchone()["id"]
                append_entry(cur, kind="USER_CREATED", status="info", actor_id=second.id,
                             details={"user_id": new_id, "role": "admin"})
        assert err.value.sqlstate == "LL014"


def test_the_database_lets_only_the_main_administrator_change_an_administrators_access():
    second, third = make_user("admin"), make_user("admin")
    with db.connection() as conn:
        cur = conn.cursor()
        for actor in (None, second):                                 # no entry at all, or by another admin
            with pytest.raises(psycopg.Error) as err:
                with conn.transaction():
                    cur.execute("UPDATE users SET is_active = false WHERE id = %s", (third.id,))
                    if actor:
                        append_entry(cur, kind="USER_STATUS", status="info", actor_id=actor.id,
                                     details={"user_id": third.id, "role": "admin", "active": False})
            assert err.value.sqlstate == "LL016", actor
    assert flags(third.username)["is_active"] is True


def test_the_switch_to_the_main_administrator_rule_is_recorded_once():
    """migrate writes one MAIN_ADMIN entry when it upgrades a database across
    version 3. The database refuses a second one (rolled back either way)."""
    details = {"user_id": admin().id, "username": admin().username}
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        append_entry(cur, kind="MAIN_ADMIN", status="info", details=details)
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                append_entry(cur, kind="MAIN_ADMIN", status="info", details=details)
        raise psycopg.Rollback()


def test_the_proof_catches_a_main_administrator_swapped_behind_the_guards():
    second = make_user("admin")
    with db.connect() as conn:
        conn.execute("ALTER TABLE users DISABLE TRIGGER users_guard")    # what a database owner could do
        try:
            conn.execute("UPDATE users SET is_main_admin = false WHERE id = %s", (admin().id,))
            conn.execute("UPDATE users SET is_main_admin = true WHERE id = %s", (second.id,))
            access = next(c for c in run_proof()["checks"] if c["id"] == "access")
            assert not access["ok"], access
            # The flag moved, but the signed record still names the real main administrator.
            assert f"{second.username} is marked as the main administrator, but the signed record names " \
                   f"{admin().username}" in access["problems"], access["problems"]
        finally:
            conn.execute("UPDATE users SET is_main_admin = false WHERE id = %s", (second.id,))
            conn.execute("UPDATE users SET is_main_admin = true WHERE id = %s", (admin().id,))
            conn.execute("ALTER TABLE users ENABLE TRIGGER users_guard")
    assert next(c for c in run_proof()["checks"] if c["id"] == "access")["ok"]


def access_problems_with(cur) -> list[str]:
    return next(c for c in proof_in(cur)["checks"] if c["id"] == "access")["problems"]


def test_the_proof_names_administrators_made_outside_the_rule():
    """What a database owner could leave behind with the commit-time check out of
    the way: an administrator made by another administrator, or a second one
    from the command line. The proof runs inside the same transaction, before
    COMMIT (where LL014 would refuse it), and everything is rolled back."""
    second = make_user("admin")
    for actor, expected in ((second.id, "was not created by the main administrator"),
                            (None, "came from the command line but is not the main administrator")):
        with db.connection() as conn, conn.transaction():
            cur = conn.cursor()
            username = unique("adm")
            cur.execute("INSERT INTO users (username, display_name, role, password_hash) "
                        "VALUES (%s, 'Off The Record', 'admin', 'x') RETURNING id", (username,))
            new_id = cur.fetchone()["id"]
            append_entry(cur, kind="USER_CREATED", status="info", actor_id=actor,
                         details={"user_id": new_id, "username": username, "role": "admin"})
            problems = access_problems_with(cur)
            assert f"administrator {username} {expected}" in " ".join(problems), problems
            raise psycopg.Rollback()                                 # leave nothing behind
    assert next(c for c in run_proof()["checks"] if c["id"] == "access")["ok"]


def test_administrators_are_judged_by_the_rule_in_force_when_they_were_created():
    """A database upgraded from version 2 keeps administrators that any administrator
    could create back then. migrate's signed MAIN_ADMIN entry (here #50) marks the
    switch: before it, another administrator is enough; after it, only the main one."""
    main, other, holder = {"id": 1, "role": "admin"}, {"id": 2, "role": "admin"}, {"id": 3, "role": "holder"}

    def problem(seq, actor, user_id=9, rule_from=50):
        creator = {1: main, 2: other, 3: holder}.get(actor)
        return admin_creation_problem({"seq": seq, "actor_id": actor}, user_id, creator, 1, rule_from)

    # before the switch: the earlier rule
    assert problem(40, 2) is None                       # made by another administrator
    assert problem(40, None) is None                    # from the command line
    assert problem(40, 3) == "was not created by another administrator"
    assert problem(40, 9) == "was not created by another administrator"            # by itself
    # after the switch: only the main administrator
    assert problem(60, 1) is None
    assert problem(60, 2) == "was not created by the main administrator"
    assert problem(60, None) == "came from the command line but is not the main administrator"
    assert problem(60, None, user_id=1) is None         # the main administrator itself
    # a database created at version 3 or later is under the new rule from its first entry
    assert problem(5, 2, rule_from=0) == "was not created by the main administrator"
    # an upgrade without the signed switch is reported by the proof; judged by the earlier rule meanwhile
    assert problem(60, 2, rule_from=None) is None
