"""Personal details: the basic details a bank keeps about each person.

Only the person changes their own details, and every change waits to be checked
again. Any administrator except that person verifies the version they looked at,
or sends it back with a note. The person and administrators see the details;
auditors see only whether they are verified. The details never enter the
permanent journal. The database enforces the same (LL017)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import psycopg
import pytest

from app import db, hooks
from app.errors import Conflict
from app.ledger import details
from app.ledger.journal import append_entry
from tests.helpers import admin, all_at_once, entries, http_login, make_user, run_proof

GOOD = {"date_of_birth": "1990-05-17", "mobile": "+91 98480 22338", "email": "Meena.K@Example.com",
        "address_line1": "Door 4-12, Gandhi Nagar", "address_line2": None, "city": "Guntur",
        "state": "Andhra Pradesh", "pin_code": "522002", "occupation": "salaried",
        "nominee_name": "Kiran Kumar", "nominee_relation": "brother"}
# The values as stored; none of them may ever appear in the journal.
PRIVATE = ("1990-05-17", "9848022338", "meena.k@example.com", "Gandhi Nagar", "Guntur", "522002", "Kiran Kumar")


def call(server: str, auth: dict, method: str, path: str, body: dict | None = None) -> httpx.Response:
    return httpx.request(method, f"{server}{path}", headers=auth, json=body, timeout=30)


def save(server: str, auth: dict, **changes) -> httpx.Response:
    return call(server, auth, "PUT", "/api/me/details", {**GOOD, **changes})


def person_with_details(server: str, role: str = "holder"):
    person = make_user(role)
    auth = http_login(server, person.username)
    r = save(server, auth)
    assert r.status_code == 200, r.text
    return person, auth


def checker(server: str) -> dict:
    """Another administrator (created by the main one), signed in."""
    return http_login(server, make_user("admin").username)


def stored(user_id: int) -> dict | None:
    with db.connect() as conn:
        return conn.execute("SELECT * FROM personal_details WHERE user_id = %s", (user_id,)).fetchone()


def details_entries(user_id: int) -> list[dict]:
    return entries("kind LIKE 'DETAILS_%%' AND details->>'user_id' = %s", (str(user_id),))


# ------------------------------------------------------------------ saving your own details

def test_a_person_saves_their_details_and_they_wait_for_a_check(server):
    person = make_user()
    auth = http_login(server, person.username)
    empty = call(server, auth, "GET", "/api/me/details").json()
    assert empty["status"] == "none" and empty["details"] is None
    assert len(empty["choices"]["states"]) == 36

    r = save(server, auth)
    assert r.status_code == 200, r.text
    mine = r.json()
    assert (mine["status"], mine["version"]) == ("waiting", 1)
    assert mine["details"]["mobile"] == "9848022338" and mine["details"]["email"] == "meena.k@example.com"

    record = details_entries(person.id)
    assert [(e["kind"], e["actor_id"], e["details"]["version"]) for e in record] == \
        [("DETAILS_SUBMITTED", person.id, 1)]
    assert "mobile" in record[0]["details"]["fields"]
    # The journal says that they changed, never what they are.
    text = json.dumps(record, default=str)
    assert not [value for value in PRIVATE if value in text], text

    again = save(server, auth)                                        # nothing changed: nothing recorded
    assert again.json()["version"] == 1 and len(details_entries(person.id)) == 1


def test_bad_details_are_refused(server):
    person = make_user()
    auth = http_login(server, person.username)
    for bad in ({"mobile": "12345"}, {"mobile": "98480 2233٨"}, {"pin_code": "022002"}, {"email": "meena@"},
                {"date_of_birth": "2015-01-01"}, {"date_of_birth": "2999-01-01"}, {"date_of_birth": "1990-02-30"},
                {"date_of_birth": 19900517}, {"state": "Atlantis"}, {"occupation": "spy"}, {"occupation": None},
                {"nominee_relation": None}, {"address_line1": "Door 4‮21"}, {"city": "Gun\x00tur"},
                {"pan": "ABCDE1234F"}, {"aadhaar": "123412341234"}, {"status": "verified"}):
        r = save(server, auth, **bad)
        assert r.status_code == 422, (bad, r.status_code, r.text)
    assert stored(person.id) is None


# ------------------------------------------------------------------ who sees them

def test_only_the_person_and_administrators_see_details(server):
    person, _ = person_with_details(server)
    other = http_login(server, make_user().username)
    auditor = http_login(server, make_user("auditor").username)
    assert call(server, other, "GET", f"/api/users/{person.username}/details").status_code == 403
    assert call(server, auditor, "GET", f"/api/users/{person.username}/details").status_code == 403
    seen = call(server, checker(server), "GET", f"/api/users/{person.username}/details").json()
    assert seen["details"]["pin_code"] == "522002"
    # Auditors see the status, nothing more.
    listing = call(server, auditor, "GET", "/api/users")
    row = next(u for u in listing.json()["users"] if u["username"] == person.username)
    assert row["details"] == "waiting"
    assert not [value for value in PRIVATE if value in listing.text]


def test_only_the_person_can_change_their_details(server):
    person, _ = person_with_details(server)
    staff = checker(server)
    for method in ("PUT", "PATCH", "POST"):                          # there is no route for it
        r = call(server, staff, method, f"/api/users/{person.username}/details", {**GOOD, "city": "Elsewhere"})
        assert r.status_code == 405, (method, r.status_code)
    assert save(server, staff, city="Tenali").status_code == 200        # an admin changes only their own
    assert stored(person.id)["city"] == "Guntur" and stored(person.id)["version"] == 1


# ------------------------------------------------------------------ checking them

def test_another_administrator_verifies_the_version_they_saw(server):
    person, auth = person_with_details(server)
    staff_user = make_user("admin")
    staff = http_login(server, staff_user.username)
    r = call(server, staff, "POST", f"/api/users/{person.username}/details/verify", {"version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "verified" and r.json()["decided_by"]["username"] == staff_user.username
    mine = call(server, auth, "GET", "/api/me/details").json()
    assert mine["status"] == "verified" and mine["decided_by"]["username"] == staff_user.username
    decision = details_entries(person.id)[-1]
    assert (decision["kind"], decision["actor_id"], decision["details"]["version"]) == \
        ("DETAILS_VERIFIED", staff_user.id, 1)


def test_nobody_checks_their_own_details(server):
    staff_user = make_user("admin")
    staff = http_login(server, staff_user.username)
    assert save(server, staff).status_code == 200
    r = call(server, staff, "POST", f"/api/users/{staff_user.username}/details/verify", {"version": 1})
    assert r.status_code == 403 and r.json()["error"]["code"] == "OWN_DETAILS", r.text
    person, auth = person_with_details(server)
    auditor = http_login(server, make_user("auditor").username)
    for who in (auth, auditor):                                      # holders and auditors never check
        r = call(server, who, "POST", f"/api/users/{person.username}/details/verify", {"version": 1})
        assert r.status_code == 403, r.text
    r = call(server, checker(server), "POST", f"/api/users/{staff_user.username}/details/verify", {"version": 1})
    assert r.status_code == 200, r.text                              # another administrator can


def test_a_check_applies_only_to_the_version_that_was_seen(server):
    person, auth = person_with_details(server)
    staff = checker(server)
    assert save(server, auth, mobile="9000012345").json()["version"] == 2      # changed while being looked at
    r = call(server, staff, "POST", f"/api/users/{person.username}/details/verify", {"version": 1})
    assert r.status_code == 409 and r.json()["error"]["code"] == "DETAILS_CHANGED", r.text
    assert stored(person.id)["status"] == "waiting"
    r = call(server, staff, "POST", f"/api/users/{person.username}/details/verify", {"version": 2})
    assert r.status_code == 200, r.text


def test_changing_verified_details_sends_them_back_for_checking(server):
    person, auth = person_with_details(server)
    staff = checker(server)
    assert call(server, staff, "POST", f"/api/users/{person.username}/details/verify", {"version": 1}).status_code == 200
    r = save(server, auth, city="Tenali")
    assert (r.json()["status"], r.json()["version"], r.json()["decided_by"]) == ("waiting", 2, None)
    latest = details_entries(person.id)[-1]
    assert (latest["kind"], latest["details"]["fields"]) == ("DETAILS_SUBMITTED", ["city"])


def test_sending_back_needs_a_note_that_stays_out_of_the_journal(server):
    person, auth = person_with_details(server)
    staff = checker(server)
    path = f"/api/users/{person.username}/details/return"
    assert call(server, staff, "POST", path, {"version": 1}).status_code == 422          # no note
    note = "PIN code 522002 does not match the address proof"
    r = call(server, staff, "POST", path, {"version": 1, "note": note})
    assert r.status_code == 200 and r.json()["status"] == "returned", r.text
    assert call(server, auth, "GET", "/api/me/details").json()["return_note"] == note       # the person sees it
    assert note not in json.dumps(details_entries(person.id), default=str)                 # the journal never does
    for verb, body in (("verify", {"version": 1}), ("return", {"version": 1, "note": "again"})):
        r = call(server, staff, "POST", f"/api/users/{person.username}/details/{verb}", body)
        assert r.status_code == 409 and r.json()["error"]["code"] == "ALREADY_DECIDED", r.text
    corrected = save(server, auth, pin_code="522003").json()
    assert (corrected["status"], corrected["version"], corrected["return_note"]) == ("waiting", 2, None)


def _waiting_details_for(person) -> None:
    details.submit(person.id, {**GOOD, "mobile": "9848022338", "email": "meena.k@example.com",
                               "date_of_birth": date(1990, 5, 17)})


def test_two_administrators_deciding_at_once_decide_once():
    for verbs in ((True, True), (True, False)):
        person = make_user()
        _waiting_details_for(person)
        checkers = (make_user("admin"), make_user("admin"))
        with hooks.race_window(after_details_check=0.3):
            results = all_at_once([lambda a=a, ok=ok: details.decide(a.id, person.username, 1, ok, None if ok else "no")
                                   for a, ok in zip(checkers, verbs)])
        losers = [r for r in results if isinstance(r, Exception)]
        assert len(losers) == 1, results
        assert isinstance(losers[0], Conflict) and losers[0].code == "ALREADY_DECIDED", losers
        assert len([e for e in details_entries(person.id) if e["kind"] != "DETAILS_SUBMITTED"]) == 1


# ------------------------------------------------------------------ the database refuses shortcuts


def test_the_database_keeps_details_on_the_record():
    person, staff, auditor = make_user(), make_user("admin"), make_user("auditor")
    _waiting_details_for(person)

    def refused(steps, code="LL017"):
        with db.connection() as conn:
            cur = conn.cursor()
            with pytest.raises(psycopg.Error) as err:
                with conn.transaction():
                    steps(cur)
            assert err.value.sqlstate == code, (err.value.sqlstate, err.value)

    def verify_as(actor_id):
        def steps(cur):
            append_entry(cur, kind="DETAILS_VERIFIED", status="info", actor_id=actor_id,
                         details={"user_id": person.id, "username": person.username, "version": 1})
            cur.execute("UPDATE personal_details SET status = 'verified', decided_by = %s, decided_at = now() "
                        "WHERE user_id = %s", (actor_id, person.id))
        return steps

    # A decision off the record, or recorded by someone who may not decide.
    refused(lambda cur: cur.execute("UPDATE personal_details SET status = 'verified', decided_by = %s, "
                                    "decided_at = now() WHERE user_id = %s", (staff.id, person.id)))
    refused(verify_as(auditor.id))
    # A change off the record, or recorded as made by someone else.
    def change_as(actor_id):
        def steps(cur):
            append_entry(cur, kind="DETAILS_SUBMITTED", status="info", actor_id=actor_id,
                         details={"user_id": person.id, "username": person.username, "version": 2, "fields": ["city"]})
            cur.execute("UPDATE personal_details SET city = 'Elsewhere', version = 2 WHERE user_id = %s", (person.id,))
        return steps
    refused(lambda cur: cur.execute("UPDATE personal_details SET city = 'Elsewhere', version = 2 "
                                    "WHERE user_id = %s", (person.id,)))
    refused(change_as(staff.id))
    # Details are corrected, never deleted; and nobody decides their own.
    refused(lambda cur: cur.execute("DELETE FROM personal_details WHERE user_id = %s", (person.id,)), "LL010")
    with db.connect() as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE personal_details SET status = 'verified', decided_by = %s, decided_at = now() "
                     "WHERE user_id = %s", (person.id, person.id))
    assert stored(person.id)["status"] == "waiting"

    # Once verified, a change cannot keep "verified", even with a decision on the record.
    with db.connection() as conn, conn.transaction():
        verify_as(staff.id)(conn.cursor())
    def edit_keeping_verified(cur):
        append_entry(cur, kind="DETAILS_VERIFIED", status="info", actor_id=staff.id,
                     details={"user_id": person.id, "username": person.username, "version": 1})
        cur.execute("UPDATE personal_details SET mobile = '9000000001' WHERE user_id = %s", (person.id,))
    refused(edit_keeping_verified)
    assert stored(person.id)["mobile"] == "9848022338"


def test_the_proof_catches_details_marked_verified_behind_the_guards():
    person = make_user()
    _waiting_details_for(person)
    with db.connect() as conn:
        conn.execute("ALTER TABLE personal_details DISABLE TRIGGER USER")      # what a database owner could do
        try:
            conn.execute("UPDATE personal_details SET status = 'verified', decided_by = %s, decided_at = now() "
                         "WHERE user_id = %s", (admin().id, person.id))
            check = next(c for c in run_proof()["checks"] if c["id"] == "details")
            assert not check["ok"] and any(person.username in p for p in check["problems"]), check
        finally:
            conn.execute("UPDATE personal_details SET status = 'waiting', decided_by = NULL, decided_at = NULL "
                         "WHERE user_id = %s", (person.id,))
            conn.execute("ALTER TABLE personal_details ENABLE TRIGGER USER")
    assert next(c for c in run_proof()["checks"] if c["id"] == "details")["ok"]
