"""The money engine. Every rupee that moves in LedgerLine moves through this file.

The shape of every money-moving request (deposit, withdraw, transfer, approve,
reject, reverse) is the same:

    BEGIN
      1. claim the idempotency key (INSERT; a duplicate waits or replays)
      2. authorisation on immutable facts (role, account owner)  -> refuse
      3. lock every customer account involved, ascending id      -> SELECT ... FOR UPDATE
      4. checks on mutable state, under those locks:
         status, daily limit, THE BALANCE CHECK                   -> refuse
      5. Sentinel risk score (advisory)                          -> maybe HOLD instead
      6. append one journal entry + its postings                 -> chain-head lock,
         inside a SAVEPOINT: if PostgreSQL refuses (floor CHECK, stale balance,
         uniqueness), roll back to the savepoint and record the refusal instead
      7. store the response on the idempotency key
    COMMIT   (deferred check: every posted entry's postings sum to zero)

"Where is the balance checked and where is the debit written, atomically?"
  * checked: step 4, `amount > accounts.available(source)`, on a row we hold
    FOR UPDATE, so nobody else can change it until we commit;
  * written: step 6, journal.append_entry() inserts the debit posting, whose
    trigger does `UPDATE accounts SET balance = balance + amount` on that
    same locked row, and the balance_floor CHECK runs inside that UPDATE;
  * both happen in one transaction: all of it commits or none of it does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Callable

import psycopg
from psycopg import Cursor

from .. import db, hooks
from ..config import get_settings
from ..errors import LedgerError, NotFound, RequestInProgress, IdempotencyKeyReused, ValidationFailed
from ..sentinel import risk
from . import accounts, idempotency, views
from .idempotency import Outcome
from .journal import Line, append_entry

log = logging.getLogger("ledgerline.engine")

# reason code -> (HTTP status, message). Every one of these is written to the journal.
REFUSALS: dict[str, tuple[int, str]] = {
    "ROLE_NOT_PERMITTED": (403, "your role is not allowed to perform this operation"),
    "NOT_ACCOUNT_OWNER": (403, "you can only move money out of your own accounts"),
    "DESTINATION_NOT_FOUND": (404, "the destination account does not exist"),
    "DESTINATION_NOT_ALLOWED": (422, "money can only be sent to a customer account"),
    "SAME_ACCOUNT": (422, "source and destination are the same account"),
    "ACCOUNT_NOT_ACTIVE": (409, "the account is frozen or closed"),
    "DESTINATION_NOT_ACTIVE": (409, "the destination account is frozen or closed"),
    "DAILY_LIMIT_EXCEEDED": (409, "this would exceed the account's daily outgoing limit"),
    "INSUFFICIENT_FUNDS": (409, "insufficient funds"),
    "HOLD_ALREADY_DECIDED": (409, "this held payment has already been approved or rejected"),
    "ALREADY_REVERSED": (409, "this entry has already been reversed"),
    "NOT_REVERSIBLE": (422, "this entry cannot be reversed"),
    "INTEGRITY_BACKSTOP": (409, "the database refused this movement; nothing was moved"),
}

REVERSIBLE_KINDS = frozenset({"DEPOSIT", "WITHDRAWAL", "TRANSFER", "HOLD_APPROVED"})


@dataclass(frozen=True)
class Actor:
    id: int
    username: str
    role: str


@dataclass(frozen=True)
class Attempt:
    """What was asked for. Recorded in the journal whether it succeeds or not."""
    kind: str
    actor: Actor
    account: dict                 # subject account (debited, or credited for deposits)
    counterparty: dict | None
    amount: int
    memo: str | None
    idem_key: str
    ref_seq: int | None = None


@dataclass
class Ctx:
    conn: psycopg.Connection
    cur: Cursor
    actor: Actor
    idem_key: str
    extra: dict = field(default_factory=dict)


class Busy(LedgerError):
    http_status = 503
    code = "BUSY"


# --------------------------------------------------------------------------- #
# The request envelope: transaction + idempotency                              #
# --------------------------------------------------------------------------- #

def run(actor: Actor, endpoint: str, payload: dict, idem_key: str,
        handler: Callable[[Ctx], Outcome]) -> Outcome:
    settings = get_settings()
    fp = idempotency.fingerprint(endpoint, payload)
    duplicate = None
    outcome: Outcome | None = None
    in_flight = False
    try:
        with db.connection() as conn:
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(f"SET LOCAL lock_timeout = '{int(settings.lock_timeout_ms)}ms'")
                cur.execute(f"SET LOCAL statement_timeout = '{int(settings.statement_timeout_ms)}ms'")
                try:
                    claimed = idempotency.claim(cur, actor.id, idem_key, endpoint, fp)
                except psycopg.errors.LockNotAvailable:
                    in_flight = True          # the first request with this key is still running
                    raise
                if claimed:
                    hooks.pause_point("after_idempotency_claim", conn)
                    outcome = handler(Ctx(conn, cur, actor, idem_key))
                    idempotency.complete(cur, actor.id, idem_key, outcome)
                else:
                    duplicate = idempotency.answer_duplicate(cur, actor.id, idem_key, endpoint, fp)
    except psycopg.errors.LockNotAvailable as exc:
        if in_flight:
            with db.connection() as conn, conn.transaction():
                idempotency.record_replay(conn.cursor(), actor.id, idem_key, endpoint,
                                          "in_flight_timeout", fp, None)
            raise RequestInProgress(
                "a request with this Idempotency-Key is still being processed; nothing was moved by "
                "this call - retry with the same key shortly", headers={"Retry-After": "1"}) from exc
        raise Busy("the system is busy; nothing was moved - retry with the same Idempotency-Key",
                   headers={"Retry-After": "1"}) from exc
    except psycopg.errors.QueryCanceled as exc:
        raise Busy("the request timed out; nothing was moved - retry with the same Idempotency-Key",
                   headers={"Retry-After": "1"}) from exc

    if duplicate is not None:
        if duplicate.mismatch:
            raise IdempotencyKeyReused(
                "this Idempotency-Key was already used for a different request; nothing was moved",
                extra={"original_endpoint": duplicate.original_endpoint})
        return duplicate.outcome
    assert outcome is not None
    return outcome


# --------------------------------------------------------------------------- #
# Building blocks                                                              #
# --------------------------------------------------------------------------- #

def _visible_balances(actor: Actor, accounts_by_id: dict[int, dict], balances: dict[int, int]) -> dict[str, int]:
    """Only show balances of accounts the caller may see."""
    shown = {}
    for account_id, balance in balances.items():
        acct = accounts_by_id.get(account_id)
        if acct is None or acct["kind"] != "customer":
            continue
        if actor.role in ("admin", "auditor") or acct["owner_id"] == actor.id:
            shown[acct["account_no"]] = balance
    return shown


def _refuse(ctx: Ctx, attempt: Attempt, reason: str, *, observe: dict | None = None,
            details: dict | None = None) -> Outcome:
    """Record a refused attempt in the journal and build the answer."""
    http_status, message = REFUSALS[reason]
    subject = observe or attempt.account
    entry = append_entry(
        ctx.cur, kind=attempt.kind, status="refused", actor_id=attempt.actor.id,
        account_id=attempt.account["id"],
        counterparty_id=attempt.counterparty["id"] if attempt.counterparty else None,
        amount=attempt.amount, idempotency_key=attempt.idem_key, reason_code=reason,
        memo=attempt.memo, ref_seq=attempt.ref_seq,
        details={**(details or {}), "observed_account_id": subject["id"]},
        observe_account_id=subject["id"])
    shown = {}
    if reason not in ("ROLE_NOT_PERMITTED", "NOT_ACCOUNT_OWNER") and entry["observed_balance"] is not None:
        shown = _visible_balances(attempt.actor, {subject["id"]: subject},
                                  {subject["id"]: entry["observed_balance"]})
    body = views.movement_body("refused", kind=attempt.kind, entry=entry, account=attempt.account,
                               counterparty=attempt.counterparty, amount=attempt.amount,
                               memo=attempt.memo, visible_balances=shown,
                               error={"code": reason, "message": message}, ref_seq=attempt.ref_seq)
    return Outcome(http_status, body, entry["seq"])


def _is_database_refusal(exc: psycopg.Error) -> bool:
    state = exc.sqlstate or ""
    return state.startswith("23") or state.startswith("LL")


def _commit(ctx: Ctx, attempt: Attempt, kind: str, lines: list[Line], *, http_status: int,
            result: str = "posted", details: dict | None = None, review: dict | None = None) -> Outcome:
    """Append the posted entry. PostgreSQL has the last word: if a constraint or
    trigger refuses (it should never need to), nothing moves and the refusal is
    recorded as INTEGRITY_BACKSTOP - a loud signal that a check upstream failed."""
    try:
        with ctx.conn.transaction():                      # SAVEPOINT
            entry = append_entry(
                ctx.cur, kind=kind, status="posted", actor_id=attempt.actor.id,
                account_id=attempt.account["id"],
                counterparty_id=attempt.counterparty["id"] if attempt.counterparty else None,
                amount=attempt.amount, idempotency_key=attempt.idem_key, memo=attempt.memo,
                ref_seq=attempt.ref_seq, details=details or {}, lines=lines)
    except psycopg.Error as exc:
        if not _is_database_refusal(exc):
            raise
        log.critical("INTEGRITY BACKSTOP: database refused %s by %s (%s): %s", kind, attempt.actor.username,
                     exc.sqlstate, exc.diag.message_primary)
        return _refuse(ctx, attempt, "INTEGRITY_BACKSTOP",
                       details={"sqlstate": exc.sqlstate, "db_message": exc.diag.message_primary})

    by_id = {line.account["id"]: line.account for line in lines}
    balances = {p["account_id"]: p["balance_after"] for p in entry["postings"]}
    body = views.movement_body(result, kind=kind, entry=entry, account=attempt.account,
                               counterparty=attempt.counterparty, amount=attempt.amount,
                               memo=attempt.memo,
                               visible_balances=_visible_balances(attempt.actor, by_id, balances),
                               review=review, ref_seq=attempt.ref_seq)
    return Outcome(http_status, body, entry["seq"])


def _daily_outgoing(cur: Cursor, account_id: int) -> int:
    """Paise already sent out of this account today (Asia/Kolkata business day).
    Read under the account's row lock, so concurrent requests cannot both fit."""
    cur.execute(
        """SELECT COALESCE(sum(-p.amount), 0)::bigint AS used
             FROM postings p JOIN journal j ON j.seq = p.entry_seq
            WHERE p.account_id = %s AND p.amount < 0
              AND j.kind IN ('TRANSFER', 'WITHDRAWAL', 'HOLD')
              AND j.created_at >= (date_trunc('day', clock_timestamp() AT TIME ZONE 'Asia/Kolkata')
                                   AT TIME ZONE 'Asia/Kolkata')""",
        (account_id,),
    )
    return cur.fetchone()["used"]


def _load_entry(cur: Cursor, seq: int) -> dict | None:
    cur.execute("SELECT * FROM journal WHERE seq = %s", (seq,))
    return cur.fetchone()


def _hold_review(assessment: risk.Assessment, actor: Actor) -> dict:
    if actor.role == "holder":
        return {"held": True, "message": views.HOLDER_HOLD_MESSAGE}   # no rule oracle for fraudsters
    return {"held": True, **assessment.record()}


# --------------------------------------------------------------------------- #
# Money in: deposit (administrator records external money arriving)            #
# --------------------------------------------------------------------------- #

def deposit(actor: Actor, account_no: str, amount: int, memo: str | None, idem_key: str) -> Outcome:
    account_no = accounts.normalise_account_no(account_no)
    payload = {"account": account_no, "amount": amount, "memo": memo or ""}

    def handler(ctx: Ctx) -> Outcome:
        cur = ctx.cur
        acct = accounts.find(cur, account_no)
        if acct is None or acct["kind"] != "customer":
            raise NotFound(f"account {account_no} not found")
        external = accounts.system(cur, "SYS-EXTERNAL")
        attempt = Attempt("DEPOSIT", actor, acct, external, amount, memo, idem_key)

        if actor.role != "admin":                                  # decided by the server, every time
            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")

        acct = accounts.lock(cur, [acct["id"]])[acct["id"]]
        attempt = replace(attempt, account=acct)
        if acct["status"] != "active":
            return _refuse(ctx, attempt, "ACCOUNT_NOT_ACTIVE")

        return _commit(ctx, attempt, "DEPOSIT", [Line(external, -amount), Line(acct, amount)],
                       http_status=201)

    return run(actor, "deposit", payload, idem_key, handler)


# --------------------------------------------------------------------------- #
# Money out: withdrawal (holder, own account)                                   #
# --------------------------------------------------------------------------- #

def withdraw(actor: Actor, account_no: str, amount: int, memo: str | None, idem_key: str) -> Outcome:
    account_no = accounts.normalise_account_no(account_no)
    payload = {"account": account_no, "amount": amount, "memo": memo or ""}

    def handler(ctx: Ctx) -> Outcome:
        cur = ctx.cur
        source = accounts.find(cur, account_no)
        if source is None or source["kind"] != "customer":
            raise NotFound(f"account {account_no} not found")
        external = accounts.system(cur, "SYS-EXTERNAL")
        attempt = Attempt("WITHDRAWAL", actor, source, external, amount, memo, idem_key)

        # Authorisation on immutable facts (owner_id cannot change: DB guard LL003).
        if actor.role != "holder":
            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")
        if source["owner_id"] != actor.id:
            return _refuse(ctx, attempt, "NOT_ACCOUNT_OWNER")

        # Lock, then decide on mutable state.
        source = accounts.lock(cur, [source["id"]])[source["id"]]
        attempt = replace(attempt, account=source)
        if source["status"] != "active":
            return _refuse(ctx, attempt, "ACCOUNT_NOT_ACTIVE")
        if _daily_outgoing(cur, source["id"]) + amount > source["daily_limit"]:
            return _refuse(ctx, attempt, "DAILY_LIMIT_EXCEEDED")
        if amount > accounts.available(source):                    # THE balance check
            return _refuse(ctx, attempt, "INSUFFICIENT_FUNDS")
        hooks.pause_point("after_balance_check", ctx.conn)

        assessment = risk.assess(cur, kind="WITHDRAWAL", source=source, destination=None,
                                 amount=amount, actor_id=actor.id)
        if assessment.hold:
            suspense = accounts.system(cur, "SYS-SUSPENSE")
            details = {"intended_kind": "WITHDRAWAL", "destination_account_id": external["id"],
                       "risk": assessment.record()}
            return _commit(ctx, attempt, "HOLD", [Line(source, -amount), Line(suspense, amount)],
                           http_status=202, result="held", details=details,
                           review=_hold_review(assessment, actor))
        return _commit(ctx, attempt, "WITHDRAWAL", [Line(source, -amount), Line(external, amount)],
                       http_status=201, details={"risk": assessment.record()})

    return run(actor, "withdraw", payload, idem_key, handler)


# --------------------------------------------------------------------------- #
# Transfer between two accounts (holder, from own account)                     #
# --------------------------------------------------------------------------- #

def transfer(actor: Actor, from_no: str, to_no: str, amount: int, memo: str | None, idem_key: str) -> Outcome:
    from_no = accounts.normalise_account_no(from_no)
    to_no = accounts.normalise_account_no(to_no)
    payload = {"from": from_no, "to": to_no, "amount": amount, "memo": memo or ""}

    def handler(ctx: Ctx) -> Outcome:
        cur = ctx.cur
        source = accounts.find(cur, from_no)
        if source is None or source["kind"] != "customer":
            raise NotFound(f"account {from_no} not found")
        destination = accounts.find(cur, to_no)
        attempt = Attempt("TRANSFER", actor, source, destination, amount, memo, idem_key)

        # Authorisation and shape, on immutable facts only (no locks taken yet,
        # so an attacker cannot hold a victim's row lock).
        if actor.role != "holder":
            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")
        if source["owner_id"] != actor.id:
            return _refuse(ctx, attempt, "NOT_ACCOUNT_OWNER")
        if destination is None:
            return _refuse(ctx, attempt, "DESTINATION_NOT_FOUND")
        if destination["kind"] != "customer":
            return _refuse(ctx, attempt, "DESTINATION_NOT_ALLOWED")
        if destination["id"] == source["id"]:
            return _refuse(ctx, attempt, "SAME_ACCOUNT")

        # Lock both rows in ascending id order: A->B and B->A cannot deadlock.
        locked = accounts.lock(cur, [source["id"], destination["id"]])
        source, destination = locked[source["id"]], locked[destination["id"]]
        attempt = replace(attempt, account=source, counterparty=destination)

        if source["status"] != "active":
            return _refuse(ctx, attempt, "ACCOUNT_NOT_ACTIVE")
        if destination["status"] != "active":
            return _refuse(ctx, attempt, "DESTINATION_NOT_ACTIVE")
        if _daily_outgoing(cur, source["id"]) + amount > source["daily_limit"]:
            return _refuse(ctx, attempt, "DAILY_LIMIT_EXCEEDED")
        if amount > accounts.available(source):                    # THE balance check
            return _refuse(ctx, attempt, "INSUFFICIENT_FUNDS")
        hooks.pause_point("after_balance_check", ctx.conn)

        assessment = risk.assess(cur, kind="TRANSFER", source=source, destination=destination,
                                 amount=amount, actor_id=actor.id)
        if assessment.hold:
            suspense = accounts.system(cur, "SYS-SUSPENSE")
            details = {"intended_kind": "TRANSFER", "destination_account_id": destination["id"],
                       "risk": assessment.record()}
            return _commit(ctx, attempt, "HOLD", [Line(source, -amount), Line(suspense, amount)],
                           http_status=202, result="held", details=details,
                           review=_hold_review(assessment, actor))
        return _commit(ctx, attempt, "TRANSFER", [Line(source, -amount), Line(destination, amount)],
                       http_status=201, details={"risk": assessment.record()})

    return run(actor, "transfer", payload, idem_key, handler)


# --------------------------------------------------------------------------- #
# Human decision on a held payment (administrator)                             #
# --------------------------------------------------------------------------- #

def decide_hold(actor: Actor, hold_seq: int, decision: str, note: str | None, idem_key: str) -> Outcome:
    if decision not in ("approve", "reject"):
        raise ValidationFailed("decision must be approve or reject")
    payload = {"hold_seq": hold_seq, "decision": decision, "note": note or ""}

    def handler(ctx: Ctx) -> Outcome:
        cur = ctx.cur
        hold = _load_entry(cur, hold_seq)
        if hold is None or hold["kind"] != "HOLD" or hold["status"] != "posted":
            raise NotFound(f"no held payment #{hold_seq}")
        source = accounts.get(cur, hold["account_id"])
        destination = accounts.get(cur, hold["details"]["destination_account_id"])
        kind = "HOLD_APPROVED" if decision == "approve" else "HOLD_REJECTED"
        attempt = Attempt(kind, actor, source, destination, hold["amount"], note, idem_key, ref_seq=hold_seq)

        if actor.role != "admin":
            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")

        # Both decisions lock the same customer rows, so approve-vs-approve and
        # approve-vs-reject on one hold are serialised; then we re-check.
        locked = accounts.lock(cur, [a["id"] for a in (source, destination) if a["kind"] == "customer"])
        source = locked.get(source["id"], source)
        destination = locked.get(destination["id"], destination)
        attempt = replace(attempt, account=source, counterparty=destination)

        cur.execute(
            "SELECT seq FROM journal WHERE ref_seq = %s AND kind IN ('HOLD_APPROVED', 'HOLD_REJECTED') "
            "AND status = 'posted'", (hold_seq,))
        if cur.fetchone() is not None:
            return _refuse(ctx, attempt, "HOLD_ALREADY_DECIDED")

        cur.execute("SELECT seq FROM journal WHERE kind = 'AI_NOTE' AND ref_seq = %s ORDER BY seq", (hold_seq,))
        ai_notes = [row["seq"] for row in cur.fetchall()]
        suggestion = hold["details"].get("risk", {})
        details = {
            "decision": decision,
            "reviewer_id": actor.id,
            "hold_seq": hold_seq,
            "suggestion": {"ruleset": suggestion.get("ruleset"), "score": suggestion.get("score"),
                           "reasons": [r["code"] for r in suggestion.get("reasons", [])]},
            "ai_note_seqs": ai_notes,
        }
        suspense = accounts.system(cur, "SYS-SUSPENSE")
        if decision == "approve":
            if destination["kind"] == "customer" and destination["status"] != "active":
                return _refuse(ctx, attempt, "DESTINATION_NOT_ACTIVE", observe=destination)
            lines = [Line(suspense, -hold["amount"]), Line(destination, hold["amount"])]
        else:
            lines = [Line(suspense, -hold["amount"]), Line(source, hold["amount"])]
        return _commit(ctx, attempt, kind, lines, http_status=201, details=details)

    return run(actor, f"hold.{decision}", payload, idem_key, handler)


# --------------------------------------------------------------------------- #
# Reversal (administrator correction: new compensating entry, never an edit)    #
# --------------------------------------------------------------------------- #

def reverse(actor: Actor, entry_seq: int, reason: str, idem_key: str) -> Outcome:
    payload = {"entry_seq": entry_seq, "reason": reason}

    def handler(ctx: Ctx) -> Outcome:
        cur = ctx.cur
        original = _load_entry(cur, entry_seq)
        if original is None:
            raise NotFound(f"no journal entry #{entry_seq}")
        if original["amount"] is None:
            raise ValidationFailed("only money movements can be reversed")
        account = accounts.get(cur, original["account_id"])
        counterparty = accounts.get(cur, original["counterparty_id"])
        attempt = Attempt("REVERSAL", actor, account, counterparty, original["amount"], reason, idem_key,
                          ref_seq=entry_seq)

        if actor.role != "admin":
            return _refuse(ctx, attempt, "ROLE_NOT_PERMITTED")
        if original["status"] != "posted" or original["kind"] not in REVERSIBLE_KINDS:
            return _refuse(ctx, attempt, "NOT_REVERSIBLE")

        # Money flows back the way it came.
        if original["kind"] == "DEPOSIT":
            pay_from, pay_to = account, counterparty          # customer -> external
        else:
            pay_from, pay_to = counterparty, account          # payee/external -> original payer

        locked = accounts.lock(cur, [a["id"] for a in (pay_from, pay_to) if a["kind"] == "customer"])
        pay_from = locked.get(pay_from["id"], pay_from)
        pay_to = locked.get(pay_to["id"], pay_to)
        if account["id"] in locked:
            attempt = replace(attempt, account=locked[account["id"]])
        if counterparty["id"] in locked:
            attempt = replace(attempt, counterparty=locked[counterparty["id"]])

        cur.execute("SELECT seq FROM journal WHERE ref_seq = %s AND kind = 'REVERSAL' AND status = 'posted'",
                    (entry_seq,))
        if cur.fetchone() is not None:
            return _refuse(ctx, attempt, "ALREADY_REVERSED")
        if pay_from["kind"] == "customer" and original["amount"] > accounts.available(pay_from):
            return _refuse(ctx, attempt, "INSUFFICIENT_FUNDS", observe=pay_from)

        details = {"reverses_seq": entry_seq, "reverses_kind": original["kind"], "reason": reason}
        return _commit(ctx, attempt, "REVERSAL",
                       [Line(pay_from, -original["amount"]), Line(pay_to, original["amount"])],
                       http_status=201, details=details)

    return run(actor, "reverse", payload, idem_key, handler)
