"""Request dependencies: who is calling, and the Idempotency-Key header.

The JWT only says WHO the caller is. Their role and whether they are still
active are re-read from the database on every request, so a demoted or
deactivated user's old token stops working immediately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import Depends, Header
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .. import db
from ..errors import Forbidden, LedgerError, Unauthorized, ValidationFailed
from ..ledger.engine import Actor
from ..security import decode_access_token

bearer = HTTPBearer(auto_error=False)
IDEMPOTENCY_KEY_RE = re.compile(r"[A-Za-z0-9_.:-]{8,100}", re.ASCII)


@dataclass(frozen=True)
class CurrentUser:
    id: int
    username: str
    display_name: str
    role: str
    is_main_admin: bool = False

    @property
    def actor(self) -> Actor:
        return Actor(self.id, self.username, self.role)


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer)) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise Unauthorized("sign in first: send Authorization: Bearer <token>")
    claims = decode_access_token(creds.credentials)
    try:
        user_id = int(claims["sub"])
    except (TypeError, ValueError) as exc:
        raise Unauthorized("invalid access token", code="TOKEN_INVALID") from exc
    with db.read_transaction() as cur:
        cur.execute("SELECT id, username, display_name, role, is_active, is_main_admin, token_version "
                    "FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    if user is None or not user["is_active"] or user["token_version"] != claims["tv"]:
        raise Unauthorized("this session is no longer valid; sign in again", code="SESSION_REVOKED")
    return CurrentUser(user["id"], user["username"], user["display_name"], user["role"], user["is_main_admin"])


def require_roles(*roles: str):
    def dependency(user: CurrentUser = Depends(current_user)) -> CurrentUser:
        if user.role not in roles:
            raise Forbidden(f"this needs one of these roles: {', '.join(roles)}")
        return user
    return dependency


def idempotency_key(key: str | None = Header(default=None, alias="Idempotency-Key")) -> str:
    if not key:
        raise LedgerError("requests that move money must carry an Idempotency-Key header",
                          code="IDEMPOTENCY_KEY_REQUIRED", http_status=428)
    if not IDEMPOTENCY_KEY_RE.fullmatch(key):
        raise ValidationFailed("Idempotency-Key must be 8-100 characters from A-Z a-z 0-9 _ . : -")
    return key
