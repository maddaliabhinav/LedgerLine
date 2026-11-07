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
        cur.execute("""SELECT u.id, u.username, u.display_name, u.role, u.is_active, u.created_at,
                              count(a.id) AS accounts
                         FROM users u LEFT JOIN accounts a ON a.owner_id = u.id
                        GROUP BY u.id ORDER BY u.id""")
        return {"users": [{"username": r["username"], "name": r["display_name"], "role": r["role"],
                           "active": r["is_active"], "accounts": r["accounts"],
                           "created_at": iso_utc(r["created_at"])} for r in cur.fetchall()]}


@router.post("/users", status_code=201)
def create_user(body: CreateUserIn, user: CurrentUser = Depends(require_roles("admin"))):
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
