from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .. import db
from ..config import get_settings
from ..errors import Forbidden, Unauthorized
from ..ledger import admin_ops
from ..money import money_view
from ..security import create_access_token, dummy_hash, login_throttle, signup_throttle, verify_password
from .deps import CurrentUser, current_user
from .schemas import LoginIn, PasswordChangeIn, SignUpIn

router = APIRouter(prefix="/api", tags=["auth"])

PERMISSIONS = {
    "admin": ["view all accounts and statements", "open accounts", "freeze / unfreeze / close accounts",
              "set overdraft limits (up to Rs 50,000)",
              "create holders and auditors, and remove or restore their access",
              "approve or decline requests for auditor access",
              "record deposits (money arriving from outside)", "approve or reject held payments",
              "reverse a posted entry (with a reason)", "run proofs, reconciliations and exports",
              "verify or send back other people's personal details"],
    "holder": ["view own accounts and statements", "withdraw from own accounts",
               "transfer from own accounts to any active customer account", "check own receipts"],
    "auditor": ["read-only: view all accounts, statements and the full journal",
                "run proofs and reconciliations, export evidence", "ask for AI review notes",
                "cannot move money or change anyone's data but their own details and password"],
}
# Everyone, whatever the role.
for _role in PERMISSIONS:
    PERMISSIONS[_role] += ["keep your own personal details", "change your own password"]
# The one main administrator: decides who is an administrator. Nobody can remove it.
MAIN_ADMIN_PERMISSIONS = ["create administrators, and remove or restore an administrator's access"]


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
        cur.execute("SELECT id, username, display_name, role, password_hash, is_active, is_main_admin, "
                    "token_version FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        request_row = None
        if user is None:
            cur.execute("SELECT status, password_hash FROM access_requests WHERE username = %s "
                        "AND status <> 'approved' ORDER BY id DESC LIMIT 1", (username,))
            request_row = cur.fetchone()
    # Always run one password hash so response time does not reveal whether the user exists.
    stored = user["password_hash"] if user else request_row["password_hash"] if request_row else dummy_hash()
    valid = verify_password(body.password, stored)
    if user is None and request_row is not None and valid:
        # The right password for an auditor request nobody has approved. Still no
        # token: say why, but only to someone who knows the password.
        if request_row["status"] == "pending":
            raise Forbidden("your request for auditor access is waiting for an administrator's approval",
                            code="APPROVAL_PENDING")
        raise Forbidden("your request for auditor access was declined by an administrator",
                        code="ACCESS_DECLINED")
    if not user or not valid or not user["is_active"]:
        login_throttle.fail(ip, username)
        raise Unauthorized("wrong username or password", code="BAD_CREDENTIALS")
    token, expires_in = create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in,
            "user": user_view(user["username"], user["display_name"], user["role"], user["is_main_admin"])}


@router.post("/auth/register", status_code=201)
def register(body: SignUpIn, request: Request):
    """Public self-service sign-up.

    role "holder" (the default): an account holder with one Rs 0 account, signed in at once (201).
    role "auditor": a REQUEST only (202, no token). Nobody can sign in with it until an
    administrator approves it. Administrator access is never self-service (422)."""
    signup_throttle.attempt(_client_ip(request))
    if body.role == "auditor":
        req = admin_ops.request_auditor_access(body.username, body.display_name, body.password)
        return JSONResponse(status_code=202, content={
            "status": "pending",
            "message": "Your request for auditor access is waiting for an administrator. "
                       "You can sign in once it is approved.",
            "request": admin_ops.access_request_view(req)})
    user, acct = admin_ops.register_holder(body.username, body.display_name, body.password)
    token, expires_in = create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in,
            "user": user_view(user["username"], user["display_name"], user["role"], False),
            "account": {"account_no": acct["account_no"], "name": acct["name"],
                        "balance": money_view(acct["balance"])}}


@router.post("/auth/logout")
def logout(user: CurrentUser = Depends(current_user)):
    """Revokes every token of this user (token_version bump)."""
    with db.connection() as conn, conn.transaction():
        conn.execute("UPDATE users SET token_version = token_version + 1 WHERE id = %s", (user.id,))
    return {"ok": True}


@router.post("/me/password")
def change_password(body: PasswordChangeIn, request: Request, user: CurrentUser = Depends(current_user)):
    """Change your own password (any signed-in user). Needs the current password;
    wrong guesses count against the same throttle as sign-in. Every other session
    ends; this one continues with the fresh token in the response."""
    ip = _client_ip(request)
    login_throttle.check(ip, user.username, what="password attempts")
    try:
        updated = admin_ops.change_password(user.id, body.current_password, body.new_password)
    except Forbidden as exc:
        if exc.code == "WRONG_PASSWORD":
            login_throttle.fail(ip, user.username)
        raise
    token, expires_in = create_access_token(updated)
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in,
            "user": user_view(updated["username"], updated["display_name"], updated["role"],
                              updated["is_main_admin"]),
            "message": "Password changed. Every other session has been signed out."}


@router.get("/me")
def me(user: CurrentUser = Depends(current_user)):
    return user_view(user.username, user.display_name, user.role, user.is_main_admin)


def user_view(username: str, name: str, role: str, main_admin: bool) -> dict:
    permissions = PERMISSIONS[role] + (MAIN_ADMIN_PERMISSIONS if main_admin else [])
    return {"username": username, "name": name, "role": role, "main_admin": main_admin, "permissions": permissions}
