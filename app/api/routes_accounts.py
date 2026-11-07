from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .. import db
from ..errors import Forbidden, NotFound
from ..ledger import accounts, admin_ops, statements
from .deps import CurrentUser, current_user, require_roles
from .schemas import OpenAccountIn, OverdraftIn, StatusIn

router = APIRouter(prefix="/api", tags=["accounts"])


def _load_visible(cur, user: CurrentUser, account_no: str) -> dict:
    """Holders see only their own accounts; admins and auditors see all."""
    acct = accounts.find(cur, accounts.normalise_account_no(account_no))
    if acct is None:
        raise NotFound(f"account {account_no} not found")
    if user.role == "holder" and acct["owner_id"] != user.id:
        raise Forbidden("you can only view your own accounts")
    return acct


@router.get("/accounts")
def list_accounts(user: CurrentUser = Depends(current_user)):
    with db.read_transaction() as cur:
        if user.role == "holder":
            cur.execute(f"SELECT {accounts.ACCOUNT_COLUMNS} FROM accounts WHERE owner_id = %s ORDER BY id", (user.id,))
        else:
            cur.execute(f"SELECT {accounts.ACCOUNT_COLUMNS} FROM accounts ORDER BY kind DESC, id")
        rows = cur.fetchall()
        return {"accounts": [statements.account_view(cur, row) for row in rows]}


@router.post("/accounts", status_code=201)
def open_account(body: OpenAccountIn, user: CurrentUser = Depends(require_roles("admin"))):
    acct = admin_ops.open_account(user.id, body.owner_username, body.name)
    with db.read_transaction() as cur:
        return statements.account_view(cur, acct)


@router.get("/accounts/{account_no}")
def get_account(account_no: str, user: CurrentUser = Depends(current_user)):
    with db.read_transaction() as cur:
        return statements.account_view(cur, _load_visible(cur, user, account_no))


@router.get("/accounts/{account_no}/statement")
def get_statement(account_no: str, user: CurrentUser = Depends(current_user),
                  limit: int = Query(100, ge=1, le=500), before_seq: int | None = Query(None, gt=0)):
    with db.read_transaction(snapshot=True) as cur:
        acct = _load_visible(cur, user, account_no)
        rows = statements.statement(cur, acct, limit=limit, before_seq=before_seq)
        return {"account": statements.account_view(cur, acct), "rows": rows,
                "next_before_seq": rows[-1]["seq"] if len(rows) == limit else None}


@router.get("/payees/{account_no}")
def payee(account_no: str, user: CurrentUser = Depends(current_user)):
    """Confirm-the-payee lookup: name and status only, never the balance."""
    with db.read_transaction() as cur:
        acct = accounts.find(cur, accounts.normalise_account_no(account_no))
        if acct is None or acct["kind"] != "customer":
            raise NotFound(f"account {account_no} not found")
        cur.execute("SELECT display_name FROM users WHERE id = %s", (acct["owner_id"],))
        owner = cur.fetchone()
        return {"account_no": acct["account_no"], "owner_name": owner["display_name"],
                "account_name": acct["name"], "status": acct["status"]}


@router.post("/accounts/{account_no}/freeze")
def freeze(account_no: str, body: StatusIn, user: CurrentUser = Depends(require_roles("admin"))):
    return _status(account_no, "frozen", body.reason, user)


@router.post("/accounts/{account_no}/unfreeze")
def unfreeze(account_no: str, body: StatusIn, user: CurrentUser = Depends(require_roles("admin"))):
    return _status(account_no, "active", body.reason, user)


@router.post("/accounts/{account_no}/close")
def close(account_no: str, body: StatusIn, user: CurrentUser = Depends(require_roles("admin"))):
    return _status(account_no, "closed", body.reason, user)


def _status(account_no: str, status: str, reason: str, user: CurrentUser) -> dict:
    acct = admin_ops.set_status(user.id, account_no, status, reason)
    with db.read_transaction() as cur:
        return statements.account_view(cur, acct)


@router.post("/accounts/{account_no}/overdraft")
def overdraft(account_no: str, body: OverdraftIn, user: CurrentUser = Depends(require_roles("admin"))):
    acct = admin_ops.set_overdraft(user.id, account_no, body.limit, body.reason)
    with db.read_transaction() as cur:
        return statements.account_view(cur, acct)
