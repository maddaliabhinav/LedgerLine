"""Personal details: the basic details a bank keeps about each person.

Only the person changes their own details. Every change is a new version that
waits to be checked. Any administrator except that person verifies a version or
sends it back with a note, once, and only the version they actually looked at.

The journal records who did what to which version, plus the names of the fields
that changed, but never a value: the journal is permanent and signed, while
personal details must stay correctable. The note that goes with "send back" is
stored with the details for the same reason (it may mention them). Personal
details are shown only to the person and to administrators.

The database enforces the same rules (LL017; see migrations/004)."""

from __future__ import annotations

import psycopg

from .. import db, hooks
from ..errors import Conflict, Forbidden, NotFound
from ..signing import iso_utc
from .journal import append_entry

FIELDS = ("date_of_birth", "mobile", "email", "address_line1", "address_line2", "city", "state", "pin_code",
          "occupation", "nominee_name", "nominee_relation")
_COLUMNS = ", ".join(FIELDS) + ", status, version, submitted_at, decided_by, decided_at, return_note"

STATES = (
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat", "Haryana",
    "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur",
    "Meghalaya", "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu", "Telangana",
    "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    # Union territories
    "Andaman and Nicobar Islands", "Chandigarh", "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
)
OCCUPATIONS = {"salaried": "Salaried", "self_employed": "Self-employed", "business": "Business owner",
               "student": "Student", "homemaker": "Homemaker", "retired": "Retired", "other": "Other"}
RELATIONS = {"spouse": "Spouse", "father": "Father", "mother": "Mother", "son": "Son", "daughter": "Daughter",
             "brother": "Brother", "sister": "Sister", "other": "Other"}


def choices() -> dict:
    """The lists the form offers (one source of truth for the UI and the validation)."""
    return {"states": list(STATES),
            "occupations": [{"value": k, "label": v} for k, v in OCCUPATIONS.items()],
            "relations": [{"value": k, "label": v} for k, v in RELATIONS.items()]}


def _user(cur, username: str) -> dict:
    cur.execute("SELECT id, username, display_name, role FROM users WHERE username = %s", (username,))
    user = cur.fetchone()
    if user is None:
        raise NotFound(f"user {username} not found")
    return user


def view(user: dict, row: dict | None, decider: dict | None = None) -> dict:
    """What the person and administrators see. (Auditors only ever get the status.)"""
    base = {"username": user["username"], "name": user["display_name"]}
    if row is None:
        return {**base, "status": "none", "version": 0, "details": None}
    details = {field: row[field] for field in FIELDS}
    details["date_of_birth"] = row["date_of_birth"].isoformat()
    return {**base, "status": row["status"], "version": row["version"], "details": details,
            "submitted_at": iso_utc(row["submitted_at"]),
            "decided_by": {"username": decider["username"], "name": decider["display_name"]} if decider else None,
            "decided_at": iso_utc(row["decided_at"]) if row["decided_at"] else None,
            "return_note": row["return_note"]}


def read(cur, username: str) -> dict:
    user = _user(cur, username)
    cur.execute(f"SELECT {_COLUMNS} FROM personal_details WHERE user_id = %s", (user["id"],))
    row = cur.fetchone()
    decider = None
    if row and row["decided_by"]:
        cur.execute("SELECT username, display_name FROM users WHERE id = %s", (row["decided_by"],))
        decider = cur.fetchone()
    return view(user, row, decider)


def submit(user_id: int, values: dict) -> dict:
    """The person saves their details: a new version, waiting to be checked.
    Saving the same details again changes nothing and records nothing."""
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT id, username, display_name, role FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        cur.execute(f"SELECT {_COLUMNS} FROM personal_details WHERE user_id = %s FOR UPDATE", (user_id,))
        row = cur.fetchone()
        if row is None:
            version = 1
            changed = [field for field in FIELDS if values.get(field) is not None]
        else:
            changed = [field for field in FIELDS if row[field] != values.get(field)]
            if not changed:
                return read(cur, user["username"])
            version = row["version"] + 1
        params = [values.get(field) for field in FIELDS]
        try:
            if row is None:
                cur.execute(f"INSERT INTO personal_details (user_id, {', '.join(FIELDS)}) "
                            f"VALUES (%s, {', '.join(['%s'] * len(FIELDS))})", [user_id, *params])
            else:
                cur.execute(f"UPDATE personal_details SET {', '.join(f'{field} = %s' for field in FIELDS)}, "
                            f"version = %s, status = 'waiting', submitted_at = clock_timestamp(), "
                            f"decided_by = NULL, decided_at = NULL, return_note = NULL WHERE user_id = %s",
                            [*params, version, user_id])
        except psycopg.errors.UniqueViolation as exc:              # two first saves at the same moment
            raise Conflict("your details were saved from somewhere else just now; reload and try again",
                           code="DETAILS_CHANGED") from exc
        # On the record: who changed what, never the values.
        append_entry(cur, kind="DETAILS_SUBMITTED", status="info", actor_id=user_id,
                     details={"user_id": user_id, "username": user["username"], "version": version,
                              "fields": changed})
        return read(cur, user["username"])


def decide(actor_id: int, username: str, version: int, verify: bool, note: str | None) -> dict:
    """An administrator verifies the version they looked at, or sends it back with a
    note. Never their own details, once per version. The row is locked first, so two
    administrators deciding at the same moment queue up and the second one sees the
    first decision; a version that changed meanwhile is refused (DETAILS_CHANGED)."""
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        user = _user(cur, username)
        if user["id"] == actor_id:
            raise Forbidden("nobody checks their own personal details; another administrator does",
                            code="OWN_DETAILS")
        cur.execute(f"SELECT {_COLUMNS} FROM personal_details WHERE user_id = %s FOR UPDATE", (user["id"],))
        row = cur.fetchone()
        if row is None:
            raise NotFound(f"{username} has not given personal details yet")
        if row["version"] != version:
            raise Conflict(f"{username} changed these details (now version {row['version']}); "
                           "open them again and check the new version", code="DETAILS_CHANGED")
        if row["status"] != "waiting":
            raise Conflict(f"version {version} of these details was already {row['status']}", code="ALREADY_DECIDED")
        hooks.pause_point("after_details_check", conn)
        append_entry(cur, kind="DETAILS_VERIFIED" if verify else "DETAILS_RETURNED", status="info",
                     actor_id=actor_id,
                     details={"user_id": user["id"], "username": user["username"], "version": version})
        cur.execute("UPDATE personal_details SET status = %s, decided_by = %s, decided_at = clock_timestamp(), "
                    "return_note = %s WHERE user_id = %s",
                    ("verified" if verify else "returned", actor_id, None if verify else note, user["id"]))
        return read(cur, username)
