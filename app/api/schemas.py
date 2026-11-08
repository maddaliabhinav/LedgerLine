"""Request bodies. extra="forbid" everywhere: an unexpected field such as
"balance", "owner_id" or "role" is rejected (422), never silently applied."""

from __future__ import annotations

import unicodedata
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from ..ledger.accounts import is_customer_no, normalise_account_no
from ..money import MAX_OVERDRAFT_PAISE, parse_amount


def _amount(value: object) -> int:
    return parse_amount(value)


def _overdraft(value: object) -> int:
    return parse_amount(value, allow_zero=True, maximum=MAX_OVERDRAFT_PAISE)


# Bidirectional-override / isolate characters can make a memo display differently
# from what it says ("Trojan Source" style spoofing in statements).
_BIDI = {0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2066, 0x2067, 0x2068, 0x2069, 0x200E, 0x200F}


def _memo(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("memo must be text")
    value = unicodedata.normalize("NFC", value).strip()
    if len(value) > 140:
        raise ValueError("memo is limited to 140 characters")
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or code in _BIDI or unicodedata.category(ch) in ("Cc", "Cs", "Co"):
            raise ValueError("memo contains control or direction-changing characters")
    return value or None


def _account_no(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("account number must be text like LL-1001")
    value = normalise_account_no(value)
    if not is_customer_no(value):
        raise ValueError("account number must look like LL-1001")
    return value


Amount = Annotated[int, BeforeValidator(_amount)]
OverdraftAmount = Annotated[int, BeforeValidator(_overdraft)]
Memo = Annotated[str | None, BeforeValidator(_memo)]
AccountNo = Annotated[str, BeforeValidator(_account_no)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginIn(Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class SignUpIn(Strict):
    """Public sign-up. role is "holder" (the default) or "auditor", which only
    files a request for an administrator to decide. {"role": "admin"} is a 422,
    and extra="forbid" rejects anything else ("balance", "is_active", ...)
    instead of silently ignoring it."""
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=10, max_length=256)
    role: Literal["holder", "auditor"] = "holder"


class DepositIn(Strict):
    account: AccountNo
    amount: Amount
    memo: Memo = None


class WithdrawIn(Strict):
    account: AccountNo
    amount: Amount
    memo: Memo = None


class TransferIn(Strict):
    from_account: AccountNo
    to_account: AccountNo
    amount: Amount
    memo: Memo = None


class DecisionIn(Strict):
    note: Memo = None


class ReverseIn(Strict):
    entry_seq: int = Field(gt=0)
    reason: Annotated[str, BeforeValidator(_memo)] = Field(min_length=3)


class OpenAccountIn(Strict):
    owner_username: str = Field(min_length=3, max_length=32)
    name: str = Field(min_length=1, max_length=60)


class StatusIn(Strict):
    reason: Annotated[str, BeforeValidator(_memo)] = Field(min_length=3)


class OverdraftIn(Strict):
    limit: OverdraftAmount
    reason: Annotated[str, BeforeValidator(_memo)] = Field(min_length=3)


class CreateUserIn(Strict):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    role: str
    password: str = Field(min_length=10, max_length=256)
