"""Endpoints that move money. Every one requires an Idempotency-Key header.

Authorisation for these is decided inside the engine (app/ledger/engine.py),
on every request, and every refusal is written to the journal - so the route
handlers deliberately do not pre-filter by role.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from ..ledger import engine
from ..ledger.idempotency import Outcome
from .deps import CurrentUser, current_user, idempotency_key
from .schemas import DecisionIn, DepositIn, ReverseIn, TransferIn, WithdrawIn

router = APIRouter(prefix="/api", tags=["money"])


def respond(outcome: Outcome) -> Response:
    """Send the exact stored bytes, so a replay is byte-for-byte the first answer."""
    headers = dict(outcome.headers)
    if outcome.entry_seq:
        headers["X-Ledger-Seq"] = str(outcome.entry_seq)
    return Response(content=outcome.render(), status_code=outcome.http_status,
                    media_type="application/json", headers=headers)


@router.post("/deposits", status_code=201)
def deposit(body: DepositIn, user: CurrentUser = Depends(current_user), key: str = Depends(idempotency_key)):
    return respond(engine.deposit(user.actor, body.account, body.amount, body.memo, key))


@router.post("/withdrawals", status_code=201)
def withdraw(body: WithdrawIn, user: CurrentUser = Depends(current_user), key: str = Depends(idempotency_key)):
    return respond(engine.withdraw(user.actor, body.account, body.amount, body.memo, key))


@router.post("/transfers", status_code=201)
def transfer(body: TransferIn, user: CurrentUser = Depends(current_user), key: str = Depends(idempotency_key)):
    return respond(engine.transfer(user.actor, body.from_account, body.to_account, body.amount, body.memo, key))


@router.post("/reversals", status_code=201)
def reverse(body: ReverseIn, user: CurrentUser = Depends(current_user), key: str = Depends(idempotency_key)):
    return respond(engine.reverse(user.actor, body.entry_seq, body.reason, key))


@router.post("/reviews/{hold_seq}/approve", status_code=201)
def approve(hold_seq: int, body: DecisionIn, user: CurrentUser = Depends(current_user),
            key: str = Depends(idempotency_key)):
    return respond(engine.decide_hold(user.actor, hold_seq, "approve", body.note, key))


@router.post("/reviews/{hold_seq}/reject", status_code=201)
def reject(hold_seq: int, body: DecisionIn, user: CurrentUser = Depends(current_user),
           key: str = Depends(idempotency_key)):
    return respond(engine.decide_hold(user.actor, hold_seq, "reject", body.note, key))
