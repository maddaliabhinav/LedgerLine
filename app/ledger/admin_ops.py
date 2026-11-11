"""Administrative actions that do not move money but change who/what can move it.

Each is journaled (hash-chained and signed) so "who authorised this" always
has an answer: opening accounts, freezing, overdraft limits, users, and the
decision on every request for auditor access.
"""

from __future__ import annotations

import re

import psycopg

from .. import db, hooks
from ..errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..money import MAX_OVERDRAFT_PAISE
from ..security import MIN_PASSWORD_LENGTH, ROLES, hash_password, verify_password
from ..signing import iso_utc
from . import accounts
from .journal import append_entry

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}", re.ASCII)
USERNAME_LOCK = 727_002          # advisory-lock namespace: "this username is being handed out"
REQUEST_COLUMNS = ("id, username, display_name, role, status, requested_at, decided_by, decided_at, "
                   "decision_note, decision_seq, user_id")


def _clean_text(value: str, field: str, max_len: int) -> str:
    value = (value or "").strip()
    if not value or len(value) > max_len or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValidationFailed(f"{field} must be 1-{max_len} printable characters")
    return value


def _new_user_fields(username: str, display_name: str, password: str) -> tuple[str, str, str]:
    username = (username or "").strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise ValidationFailed("username must be 3-32 characters: a-z 0-9 _ . - (starting with a letter or digit)")
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    display_name = _clean_text(display_name, "display name", 80)
    return username, display_name, hash_password(password)   # slow on purpose; outside any transaction


def _claim_username(cur, username: str, *, request_id: int | None = None) -> None:
    """A username is unique across users AND auditor requests still waiting for a
    decision. That spans two tables, so no single UNIQUE index can cover it:
    every path that hands out a name takes the same per-name transaction lock
    first, then checks both tables. (users.username UNIQUE stays the backstop.)"""
    cur.execute("SELECT pg_advisory_xact_lock(%s::int, hashtext(%s))", (USERNAME_LOCK, username))
    cur.execute("SELECT 1 FROM users WHERE username = %s", (username,))
    if cur.fetchone() is not None:
        raise Conflict(f"username {username} is taken")
    cur.execute("SELECT id FROM access_requests WHERE username = %s AND status = 'pending'", (username,))
    waiting = cur.fetchone()
    if waiting is not None and waiting["id"] != request_id:
        raise Conflict(f"username {username} is taken")


def register_holder(username: str, display_name: str, password: str) -> tuple[dict, dict]:
    """Public self-service sign-up. The role is NOT a parameter: whoever calls
    this gets an account holder, never an admin or auditor, plus one account
    that opens at Rs 0 (money only arrives through an admin-recorded deposit).
    User, account and both journal entries commit together or not at all."""
    username, display_name, password_hash = _new_user_fields(username, display_name, password)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        _claim_username(cur, username)
        try:
            with conn.transaction():
                cur.execute(
                    "INSERT INTO users (username, display_name, role, password_hash) "
                    "VALUES (%s, %s, 'holder', %s) "
                    "RETURNING id, username, display_name, role, is_active, token_version, created_at",
                    (username, display_name, password_hash))
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"username {username} is taken") from exc
        user = cur.fetchone()
        append_entry(cur, kind="USER_CREATED", status="info", actor_id=user["id"],
                     details={"user_id": user["id"], "username": username, "role": user["role"],
                              "display_name": display_name, "via": "self-service sign-up"})
        cur.execute(
            f"INSERT INTO accounts (account_no, owner_id, name, kind) "
            f"VALUES ('LL-' || nextval('account_no_seq'), %s, 'Main account', 'customer') "
            f"RETURNING {accounts.ACCOUNT_COLUMNS}", (user["id"],))
        acct = cur.fetchone()
        append_entry(cur, kind="ACCOUNT_OPENED", status="info", actor_id=user["id"], account_id=acct["id"],
                     details={"account_id": acct["id"], "account_no": acct["account_no"], "kind": "customer",
                              "owner_id": user["id"], "owner_username": username, "name": acct["name"],
                              "overdraft_limit": 0, "daily_limit": acct["daily_limit"],
                              "via": "self-service sign-up"})
    return user, acct


def _is_main_admin(cur, user_id: int) -> bool:
    cur.execute("SELECT is_main_admin FROM users WHERE id = %s", (user_id,))
    row = cur.fetchone()
    return bool(row and row["is_main_admin"])


def create_user(actor_id: int | None, username: str, display_name: str, role: str, password: str) -> dict:
    """Creates a user on the record. Administrators come only from the main
    administrator; the main administrator itself is the one administrator made
    from the server's own command line (actor None), when there is none yet.
    The database enforces the same (LL014, one main administrator)."""
    if role not in ROLES:
        raise ValidationFailed(f"role must be one of {', '.join(ROLES)}")
    username, display_name, password_hash = _new_user_fields(username, display_name, password)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        main_admin = False
        if role == "admin":
            if actor_id is None:
                cur.execute("SELECT 1 FROM users WHERE is_main_admin")
                if cur.fetchone() is not None:
                    raise Conflict("there is already a main administrator; new administrators are created by them")
                main_admin = True
            else:
                creator_is_main = _is_main_admin(cur, actor_id)
                if not creator_is_main:
                    raise Forbidden("only the main administrator can create administrators", code="MAIN_ADMIN_ONLY")
        _claim_username(cur, username)
        try:
            with conn.transaction():
                cur.execute(
                    "INSERT INTO users (username, display_name, role, password_hash, is_main_admin) "
                    "VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING id, username, display_name, role, is_active, is_main_admin, created_at",
                    (username, display_name, role, password_hash, main_admin))
        except psycopg.errors.UniqueViolation as exc:
            if exc.diag.constraint_name == "users_one_main_admin":
                raise Conflict("there is already a main administrator") from exc
            raise Conflict(f"username {username} is taken") from exc
        user = cur.fetchone()
        details = {"user_id": user["id"], "username": username, "role": role, "display_name": display_name}
        if main_admin:
            details["main_admin"] = True
        append_entry(cur, kind="USER_CREATED", status="info", actor_id=actor_id, details=details)
    return user


def request_auditor_access(username: str, display_name: str, password: str) -> dict:
    """Public sign-up as an auditor. This creates a REQUEST, not a user: there is
    nothing to sign in with until an administrator approves it, and an
    administrator may decline it. Only auditor access can be requested (the
    database CHECK says so too); administrator access is never self-service.
    Nothing is journaled yet: asking is not an event in the books, the
    administrator's decision is."""
    username, display_name, password_hash = _new_user_fields(username, display_name, password)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        _claim_username(cur, username)
        try:
            with conn.transaction():
                cur.execute(f"INSERT INTO access_requests (username, display_name, role, password_hash) "
                            f"VALUES (%s, %s, 'auditor', %s) RETURNING {REQUEST_COLUMNS}",
                            (username, display_name, password_hash))
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"username {username} is taken") from exc
        return cur.fetchone()


def decide_access_request(actor_id: int, request_id: int, approve: bool,
                          note: str | None) -> tuple[dict, dict | None, int]:
    """An administrator approves or declines an auditor request, exactly once.

    The request row is locked first (FOR UPDATE), so two administrators deciding
    at the same moment queue up and the second one sees the first decision. The
    decision is a signed ACCESS_DECISION entry. On approval the auditor is
    created in the same transaction, from the request (never from the caller's
    input), and its USER_CREATED entry points back at the decision. The
    database refuses a decision that is not on the record, or not made by an
    administrator (LL012), and an auditor not created by one (LL014)."""
    decision = "approved" if approve else "declined"
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute(f"SELECT {REQUEST_COLUMNS}, password_hash FROM access_requests WHERE id = %s FOR UPDATE",
                    (request_id,))
        req = cur.fetchone()
        if req is None:
            raise NotFound(f"access request {request_id} not found")
        if req["status"] != "pending":
            raise Conflict(f"access request {request_id} was already {req['status']}", code="ALREADY_DECIDED")
        hooks.pause_point("after_request_check", conn)
        if approve:
            _claim_username(cur, req["username"], request_id=req["id"])
        entry = append_entry(cur, kind="ACCESS_DECISION", status="info", actor_id=actor_id, memo=note,
                             details={"request_id": req["id"], "username": req["username"],
                                      "display_name": req["display_name"], "role": req["role"],
                                      "decision": decision, "requested_at": iso_utc(req["requested_at"])})
        user = None
        if approve:
            cur.execute("INSERT INTO users (username, display_name, role, password_hash) "
                        "VALUES (%s, %s, 'auditor', %s) "
                        "RETURNING id, username, display_name, role, is_active, created_at",
                        (req["username"], req["display_name"], req["password_hash"]))
            user = cur.fetchone()
            append_entry(cur, kind="USER_CREATED", status="info", actor_id=actor_id, ref_seq=entry["seq"],
                         details={"user_id": user["id"], "username": user["username"], "role": user["role"],
                                  "display_name": user["display_name"], "via": "approved access request",
                                  "access_request_id": req["id"]})
        cur.execute(f"UPDATE access_requests SET status = %s, decided_by = %s, decided_at = clock_timestamp(), "
                    f"decision_note = %s, decision_seq = %s, user_id = %s, "
                    f"password_hash = CASE WHEN %s THEN NULL ELSE password_hash END "
                    f"WHERE id = %s RETURNING {REQUEST_COLUMNS}",
                    (decision, actor_id, note, entry["seq"], user["id"] if user else None, approve, req["id"]))
        decided = cur.fetchone()
    return decided, user, entry["seq"]


def access_request_view(row: dict, decider: dict | None = None) -> dict:
    return {"id": row["id"], "username": row["username"], "name": row["display_name"], "role": row["role"],
            "status": row["status"], "requested_at": iso_utc(row["requested_at"]),
            "decided_at": iso_utc(row["decided_at"]) if row["decided_at"] else None,
            "decided_by": decider, "note": row["decision_note"], "decision_seq": row["decision_seq"]}


def change_password(user_id: int, current: str, new: str) -> dict:
    """A person changes their own password, proving they know the current one.

    Every session of theirs ends (the token version moves on), so a stolen session
    dies with the old password; the caller gets a fresh token. The change is on the
    record as PASSWORD_CHANGED (who and when, never the password or its hash), and
    the database refuses a password change that is not (LL018)."""
    if len(new) < MIN_PASSWORD_LENGTH:
        raise ValidationFailed(f"the new password needs at least {MIN_PASSWORD_LENGTH} characters")
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash FROM users WHERE id = %s FOR UPDATE", (user_id,))
        user = cur.fetchone()
        if user is None or not verify_password(current, user["password_hash"]):
            raise Forbidden("your current password is not right", code="WRONG_PASSWORD")
        if verify_password(new, user["password_hash"]):
            raise ValidationFailed("choose a new password, not the one you have now", code="SAME_PASSWORD")
        cur.execute("UPDATE users SET password_hash = %s, token_version = token_version + 1 WHERE id = %s "
                    "RETURNING id, username, display_name, role, is_main_admin, token_version",
                    (hash_password(new), user_id))
        updated = cur.fetchone()
        append_entry(cur, kind="PASSWORD_CHANGED", status="info", actor_id=user_id,
                     details={"user_id": user_id, "username": user["username"]})
    return updated


def set_user_active(actor_id: int, username: str, active: bool) -> dict:
    """Removes or restores someone's access. Nothing is deleted: the ledger keeps
    who did what forever. Any administrator manages holders and auditors; only
    the main administrator manages administrators; nobody can remove the main
    administrator (database: LL015, LL016)."""
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT id, role, is_active, is_main_admin FROM users WHERE username = %s FOR UPDATE",
                    (username,))
        user = cur.fetchone()
        if user is None:
            raise NotFound(f"user {username} not found")
        if user["is_main_admin"] and not active:
            raise Forbidden("the main administrator's access cannot be removed", code="MAIN_ADMIN_PROTECTED")
        if user["id"] == actor_id and not active:
            raise Conflict("you cannot deactivate yourself")
        actor_is_main = _is_main_admin(cur, actor_id)
        if user["role"] == "admin" and not actor_is_main:
            raise Forbidden("only the main administrator can remove or restore an administrator's access",
                            code="MAIN_ADMIN_ONLY")
        if user["is_active"] == active:
            raise Conflict(f"user {username} is already {'active' if active else 'inactive'}")
        # Bumping token_version revokes every token issued to this user.
        cur.execute("UPDATE users SET is_active = %s, token_version = token_version + 1 WHERE id = %s "
                    "RETURNING id, username, role, is_active", (active, user["id"]))
        updated = cur.fetchone()
        append_entry(cur, kind="USER_STATUS", status="info", actor_id=actor_id,
                     details={"user_id": user["id"], "username": username, "role": user["role"], "active": active})
    return updated


def open_account(actor_id: int, owner_username: str, name: str) -> dict:
    name = _clean_text(name, "account name", 60)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT id, username, role, is_active FROM users WHERE username = %s",
                    ((owner_username or "").strip().lower(),))
        owner = cur.fetchone()
        if owner is None or owner["role"] != "holder" or not owner["is_active"]:
            raise ValidationFailed("accounts can only be opened for an active account holder")
        # balance is not a parameter: it starts at 0 (DB guard LL001) and only postings change it.
        cur.execute(
            f"INSERT INTO accounts (account_no, owner_id, name, kind) "
            f"VALUES ('LL-' || nextval('account_no_seq'), %s, %s, 'customer') RETURNING {accounts.ACCOUNT_COLUMNS}",
            (owner["id"], name))
        acct = cur.fetchone()
        append_entry(cur, kind="ACCOUNT_OPENED", status="info", actor_id=actor_id, account_id=acct["id"],
                     details={"account_id": acct["id"], "account_no": acct["account_no"], "kind": "customer",
                              "owner_id": owner["id"], "owner_username": owner["username"], "name": name,
                              "overdraft_limit": 0, "daily_limit": acct["daily_limit"]})
    return acct


def set_status(actor_id: int, account_no: str, status: str, reason: str) -> dict:
    if status not in ("active", "frozen", "closed"):
        raise ValidationFailed("status must be active, frozen or closed")
    reason = _clean_text(reason, "reason", 140)
    account_no = accounts.normalise_account_no(account_no)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        acct = accounts.find(cur, account_no)
        if acct is None or acct["kind"] != "customer":
            raise NotFound(f"account {account_no} not found")
        acct = accounts.lock(cur, [acct["id"]])[acct["id"]]   # same lock the money engine takes
        if acct["status"] == "closed":
            raise Conflict("closed accounts cannot be changed")
        if acct["status"] == status:
            raise Conflict(f"account is already {status}")
        if status == "closed":
            cur.execute("""SELECT count(*) AS n FROM journal h
                            WHERE h.kind = 'HOLD' AND h.status = 'posted' AND h.account_id = %s
                              AND NOT EXISTS (SELECT 1 FROM journal d WHERE d.ref_seq = h.seq
                                               AND d.kind IN ('HOLD_APPROVED','HOLD_REJECTED')
                                               AND d.status = 'posted')""", (acct["id"],))
            if acct["balance"] != 0 or cur.fetchone()["n"]:
                raise Conflict("only an account with a zero balance and no payments under review can be closed")
        cur.execute(f"UPDATE accounts SET status = %s WHERE id = %s RETURNING {accounts.ACCOUNT_COLUMNS}",
                    (status, acct["id"]))
        updated = cur.fetchone()
        append_entry(cur, kind="ACCOUNT_STATUS", status="info", actor_id=actor_id, account_id=acct["id"],
                     details={"account_no": account_no, "from": acct["status"], "to": status, "reason": reason})
    return updated


def set_overdraft(actor_id: int, account_no: str, limit_paise: int, reason: str) -> dict:
    """An authorised overdraft: the floor becomes -limit, enforced by the balance_floor CHECK."""
    if not 0 <= limit_paise <= MAX_OVERDRAFT_PAISE:
        raise ValidationFailed("overdraft limit must be between Rs 0 and Rs 50,000")
    reason = _clean_text(reason, "reason", 140)
    account_no = accounts.normalise_account_no(account_no)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        acct = accounts.find(cur, account_no)
        if acct is None or acct["kind"] != "customer":
            raise NotFound(f"account {account_no} not found")
        acct = accounts.lock(cur, [acct["id"]])[acct["id"]]
        try:
            with conn.transaction():
                cur.execute(f"UPDATE accounts SET overdraft_limit = %s WHERE id = %s "
                            f"RETURNING {accounts.ACCOUNT_COLUMNS}", (limit_paise, acct["id"]))
        except psycopg.errors.CheckViolation as exc:
            raise Conflict("the account is already below the floor this limit would set") from exc
        updated = cur.fetchone()
        append_entry(cur, kind="OVERDRAFT_LIMIT", status="info", actor_id=actor_id, account_id=acct["id"],
                     details={"account_no": account_no, "from": acct["overdraft_limit"], "to": limit_paise,
                              "reason": reason})
    return updated
