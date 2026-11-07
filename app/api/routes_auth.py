from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from .. import db
from ..config import get_settings
from ..errors import Unauthorized
from ..security import create_access_token, dummy_hash, login_throttle, verify_password
from .deps import CurrentUser, current_user
from .schemas import LoginIn

router = APIRouter(prefix="/api", tags=["auth"])

PERMISSIONS = {
    "admin": ["view all accounts and statements", "open accounts", "freeze / unfreeze / close accounts",
              "set overdraft limits (up to Rs 50,000)", "create and deactivate users",
              "record deposits (money arriving from outside)", "approve or reject held payments",
              "reverse a posted entry (with a reason)", "run proofs, reconciliations and exports"],
    "holder": ["view own accounts and statements", "withdraw from own accounts",
               "transfer from own accounts to any active customer account", "check own receipts"],
    "auditor": ["read-only: view all accounts, statements and the full journal",
                "run proofs and reconciliations, export evidence", "ask for AI review notes",
                "cannot move money or change anything"],
}


def _client_ip(request: Request) -> str:
    """The address used for throttling. Clients can put anything in
    X-Forwarded-For, so only the LAST hop (appended by our own hosting proxy)
    is trusted, and only when TRUST_PROXY is on (default in production)."""
    if get_settings().trust_proxy:
        hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
        if hops:
            return hops[-1]
    return request.client.host if request.client else "unknown"


@router.post("/auth/login")
def login(body: LoginIn, request: Request):
    ip = _client_ip(request)
    username = body.username.strip().lower()
    login_throttle.check(ip, username)
    with db.read_transaction() as cur:
        cur.execute("SELECT id, username, display_name, role, password_hash, is_active, token_version "
                    "FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
    # Always run one password hash so response time does not reveal whether the user exists.
    valid = verify_password(body.password, user["password_hash"] if user else dummy_hash())
    if not user or not valid or not user["is_active"]:
        login_throttle.fail(ip, username)
        raise Unauthorized("wrong username or password", code="BAD_CREDENTIALS")
    token, expires_in = create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in,
            "user": {"username": user["username"], "name": user["display_name"], "role": user["role"],
                     "permissions": PERMISSIONS[user["role"]]}}


@router.post("/auth/logout")
def logout(user: CurrentUser = Depends(current_user)):
    """Revokes every token of this user (token_version bump)."""
    with db.connection() as conn, conn.transaction():
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user.id,))
    return {"ok": True}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return {"username": user.username, "name": user.display_name, "role": user.role,
            "permissions": PERMISSIONS[user.role]}
