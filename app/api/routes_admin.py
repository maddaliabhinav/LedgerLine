from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..ledger import admin_ops
from ..signing import iso_utc
from .deps import CurrentUser, require_roles
from .schemas import CreateUserIn

router = APIRouter(prefix="/api", tags=["users"])


@router.get("/users")
def list_users(user: CurrentUser = Depends(require_roles("admin", "auditor"))):
    with db.read_transaction() as cur:
        # Personal details: only their status here (auditors never see the details themselves).
        cur.execute("""SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.is_main_admin, u.created_at,
                              (SELECT count(*) FROM accounts a WHERE a.owner_id = u.id) AS accounts,
                              coalesce(d.status, 'none') AS details, d.version AS details_version
                         FROM users u LEFT JOIN personal_details d ON d.user_id = u.id
                        ORDER BY u.id""")
        return {"users": [{"username": r["username"], "name": r["display_name"], "role": r["role"],
                           "main_admin": r["is_main_admin"], "active": r["is_active"], "accounts": r["accounts"],
                           "details": r["details"], "details_version": r["details_version"],
                           "created_at": iso_utc(r["created_at"])} for r in cur.fetchall()]}


@router.post("/users", status_code=201)
def create_user(body: CreateUserIn, user: CurrentUser = Depends(require_roles("admin"))):
    """The only way to make an auditor or an administrator: an administrator creates
    them here, and they can sign in straight away (holders can also sign themselves
    up). Administrators only from the main administrator; the database refuses staff
    not created on the record by the right administrator (LL014)."""
    created = admin_ops.create_user(user.id, body.username, body.display_name, body.role, body.password)
    return {"username": created["username"], "name": created["display_name"], "role": created["role"]}


@router.post("/users/{username}/deactivate")
def deactivate(username: str, user: CurrentUser = Depends(require_roles("admin"))):
    updated = admin_ops.set_user_active(user.id, username.strip().lower(), False)
    return {"username": updated["username"], "active": updated["is_active"]}


@router.post("/users/{username}/activate")
def activate(username: str, user: CurrentUser = Depends(require_roles("admin"))):
    updated = admin_ops.set_user_active(user.id, username.strip().lower(), True)
    return {"username": updated["username"], "active": updated["is_active"]}
