from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path

from .. import db
from ..ledger import admin_ops
from ..signing import iso_utc
from .deps import CurrentUser, require_roles
from .schemas import CreateUserIn, DecisionIn

router = APIRouter(prefix="/api", tags=["users"])

# Only an administrator decides who becomes an auditor. (The database also
# refuses a decision whose journal entry is not by an administrator: LL012.)
deciders = require_roles("admin")
RequestId = Path(gt=0, le=9_223_372_036_854_775_807)


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


@router.get("/access-requests")
def list_access_requests(state: Literal["pending", "decided"] = "pending",
                         user: CurrentUser = Depends(require_roles("admin", "auditor"))):
    """Requests for auditor access made at sign-up. Read by admins and auditors."""
    where = "r.status = 'pending' ORDER BY r.id" if state == "pending" else \
        "r.status <> 'pending' ORDER BY r.decided_at DESC, r.id DESC LIMIT 100"
    with db.read_transaction() as cur:
        cur.execute(f"""SELECT r.id, r.username, r.display_name, r.role, r.status, r.requested_at, r.decided_by,
                               r.decided_at, r.decision_note, r.decision_seq, r.user_id,
                               d.username AS decider_username, d.display_name AS decider_name
                          FROM access_requests r LEFT JOIN users d ON d.id = r.decided_by
                         WHERE {where}""")
        rows = cur.fetchall()
    return {"requests": [admin_ops.access_request_view(
        r, {"username": r["decider_username"], "name": r["decider_name"]} if r["decided_by"] else None)
        for r in rows]}


def _decided(user: CurrentUser, request_id: int, approve: bool, body: DecisionIn | None) -> dict:
    decided, created, seq = admin_ops.decide_access_request(user.id, request_id, approve, body.note if body else None)
    return {"request": admin_ops.access_request_view(decided, {"username": user.username, "name": user.display_name}),
            "user": {"username": created["username"], "name": created["display_name"], "role": created["role"]}
            if created else None,
            "entry": {"seq": seq}}


@router.post("/access-requests/{request_id}/approve")
def approve_access(request_id: int = RequestId, body: DecisionIn | None = None,
                   user: CurrentUser = Depends(deciders)):
    """Creates the auditor (from the request, never from this body) and records the decision."""
    return _decided(user, request_id, True, body)


@router.post("/access-requests/{request_id}/decline")
def decline_access(request_id: int = RequestId, body: DecisionIn | None = None,
                   user: CurrentUser = Depends(deciders)):
    return _decided(user, request_id, False, body)
