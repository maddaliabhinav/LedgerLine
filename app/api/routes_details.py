"""Personal details. The person and administrators see them in full; auditors
see only whether they are verified (in GET /api/users). Only the person changes
them; another administrator verifies or sends back a version."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .. import db
from ..ledger import details
from .deps import CurrentUser, current_user, require_roles
from .schemas import DetailsDecisionIn, DetailsIn, DetailsReturnIn

router = APIRouter(prefix="/api", tags=["personal details"])
checkers = require_roles("admin")


@router.get("/me/details")
def my_details(user: CurrentUser = Depends(current_user)):
    """Your own details (or status "none"), plus the choices the form offers."""
    with db.read_transaction() as cur:
        return {**details.read(cur, user.username), "choices": details.choices()}


@router.put("/me/details")
def save_my_details(body: DetailsIn, user: CurrentUser = Depends(current_user)):
    """Save your details. Any change is a new version that waits to be checked again."""
    return {**details.submit(user.id, body.model_dump()), "choices": details.choices()}


@router.get("/users/{username}/details")
def someones_details(username: str, user: CurrentUser = Depends(checkers)):
    with db.read_transaction() as cur:
        return details.read(cur, username.strip().lower())


@router.post("/users/{username}/details/verify")
def verify_details(username: str, body: DetailsDecisionIn, user: CurrentUser = Depends(checkers)):
    return details.decide(user.id, username.strip().lower(), body.version, True, None)


@router.post("/users/{username}/details/return")
def send_details_back(username: str, body: DetailsReturnIn, user: CurrentUser = Depends(checkers)):
    return details.decide(user.id, username.strip().lower(), body.version, False, body.note)
