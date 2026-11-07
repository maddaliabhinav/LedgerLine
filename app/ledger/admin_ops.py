"""Administrative actions that do not move money but change who/what can move it.

Each is journaled (hash-chained and signed) so "who authorised this" always
has an answer: opening accounts, freezing, overdraft limits, users.
"""

from __future__ import annotations

import re

import psycopg

from .. import db
from ..errors import Conflict, NotFound, ValidationFailed
from ..money import MAX_OVERDRAFT_PAISE
from ..security import MIN_PASSWORD_LENGTH, ROLES, hash_password
from . import accounts
from .journal import append_entry

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}", re.ASCII)


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


def register_holder(username: str, display_name: str, password: str) -> tuple[dict, dict]:
    """Public self-service sign-up. The role is NOT a parameter: whoever calls
    this gets an account holder, never an admin or auditor, plus one account
    that opens at Rs 0 (money only arrives through an admin-recorded deposit).
    User, account and both journal entries commit together or not at all."""
    username, display_name, password_hash = _new_user_fields(username, display_name, password)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
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


def create_user(actor_id: int | None, username: str, display_name: str, role: str, password: str) -> dict:
    if role not in ROLES:
        raise ValidationFailed(f"role must be one of {', '.join(ROLES)}")
    username, display_name, password_hash = _new_user_fields(username, display_name, password)
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        try:
            with conn.transaction():
                cur.execute(
                    "INSERT INTO users (username, display_name, role, password_hash) VALUES (%s, %s, %s, %s) "
                    "RETURNING id, username, display_name, role, is_active, created_at",
                    (username, display_name, role, password_hash))
        except psycopg.errors.UniqueViolation as exc:
            raise Conflict(f"username {username} is taken") from exc
        user = cur.fetchone()
        append_entry(cur, kind="USER_CREATED", status="info", actor_id=actor_id,
                     details={"user_id": user["id"], "username": username, "role": role,
                              "display_name": display_name})
    return user


def set_user_active(actor_id: int, username: str, active: bool) -> dict:
    with db.connection() as conn, conn.transaction():
        cur = conn.cursor()
        cur.execute("SELECT id, is_active FROM users WHERE username = %s FOR UPDATE", (username,))
        user = cur.fetchone()
        if user is None:
            raise NotFound(f"user {username} not found")
        if user["id"] == actor_id and not active:
            raise Conflict("you cannot deactivate yourself")
        if user["is_active"] == active:
            raise Conflict(f"user {username} is already {'active' if active else 'inactive'}")
        # Bumping token_version revokes every token issued to this user.
        cur.execute("UPDATE users SET is_active = %s, token_version = token_version + 1 WHERE id = %s "
                    "RETURNING id, username, role, is_active", (active, user["id"]))
        updated = cur.fetchone()
        append_entry(cur, kind="USER_STATUS", status="info", actor_id=actor_id,
                     details={"user_id": user["id"], "username": username, "active": active})
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
