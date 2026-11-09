"""Request bodies. extra="forbid" everywhere: an unexpected field such as
"balance", "owner_id" or "role" is rejected (422), never silently applied."""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from ..ledger.accounts import is_customer_no, normalise_account_no
from ..ledger.details import OCCUPATIONS, RELATIONS, STATES
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


# ------------------------------------------------------------------ personal details
# ASCII-only patterns: Python's \d also matches other scripts' digits ("١٢٣").
_MOBILE = re.compile(r"[6-9][0-9]{9}", re.ASCII)
_PIN = re.compile(r"[1-9][0-9]{5}", re.ASCII)
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}", re.ASCII)
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def _plain(value: object, field: str, limit: int, required: bool = True) -> str | None:
    """One line of text a person typed: no control or direction-changing characters."""
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field} must be text")
    value = " ".join(unicodedata.normalize("NFC", value).split())
    if len(value) > limit:
        raise ValueError(f"{field} is limited to {limit} characters")
    for ch in value:
        code = ord(ch)
        if code < 32 or code == 127 or code in _BIDI or unicodedata.category(ch) in ("Cc", "Cs", "Co"):
            raise ValueError(f"{field} contains control or direction-changing characters")
    return value


def _date_of_birth(value: object) -> date:
    if not isinstance(value, str) or not _ISO_DATE.fullmatch(value):
        raise ValueError("date of birth must be a date like 1996-04-12")
    try:
        born = date.fromisoformat(value)
    except ValueError:
        raise ValueError("date of birth is not a real date") from None
    today = date.today()
    age = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    if not 18 <= age <= 120:
        raise ValueError("you must be at least 18 (and the date must be in the past)")
    return born


def _mobile(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("mobile number must be text")
    digits = re.sub(r"[ ()-]", "", value)
    if digits.startswith("+91"):
        digits = digits[3:]
    elif len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if not _MOBILE.fullmatch(digits):
        raise ValueError("mobile number must be a 10-digit Indian mobile number starting with 6, 7, 8 or 9")
    return digits


def _email(value: object) -> str:
    if not isinstance(value, str) or len(value.strip()) > 254 or not _EMAIL.fullmatch(value.strip()):
        raise ValueError("email must look like name@example.com")
    return value.strip().lower()


def _pin_code(value: object) -> str:
    if not isinstance(value, str) or not _PIN.fullmatch(value.strip()):
        raise ValueError("PIN code must be 6 digits and cannot start with 0")
    return value.strip()


def _state(value: object) -> str:
    if not isinstance(value, str) or value not in STATES:
        raise ValueError("state must be one of the Indian states or union territories")
    return value


def _choice(options: dict, field: str, required: bool = False):
    def check(value: object) -> str | None:
        if value is None or value == "":
            if required:
                raise ValueError(f"{field} is required")
            return None
        if not isinstance(value, str) or value not in options:
            raise ValueError(f"{field} must be one of: {', '.join(options)}")
        return value
    return check


class DetailsIn(Strict):
    """A person's own details. No ID numbers. Unknown fields are refused (422)."""
    date_of_birth: Annotated[date, BeforeValidator(_date_of_birth)]
    mobile: Annotated[str, BeforeValidator(_mobile)]
    email: Annotated[str, BeforeValidator(_email)]
    address_line1: Annotated[str, BeforeValidator(lambda v: _plain(v, "address line 1", 120))]
    address_line2: Annotated[str | None, BeforeValidator(lambda v: _plain(v, "address line 2", 120, False))] = None
    city: Annotated[str, BeforeValidator(lambda v: _plain(v, "city", 60))]
    state: Annotated[str, BeforeValidator(_state)]
    pin_code: Annotated[str, BeforeValidator(_pin_code)]
    occupation: Annotated[str, BeforeValidator(_choice(OCCUPATIONS, "occupation", required=True))]
    nominee_name: Annotated[str | None, BeforeValidator(lambda v: _plain(v, "nominee name", 80, False))] = None
    nominee_relation: Annotated[str | None, BeforeValidator(_choice(RELATIONS, "nominee relation"))] = None

    @model_validator(mode="after")
    def _nominee_complete(self):
        if (self.nominee_name is None) != (self.nominee_relation is None):
            raise ValueError("give both the nominee's name and their relation, or neither")
        return self


class DetailsDecisionIn(Strict):
    """Verify or send back exactly the version the administrator looked at."""
    version: int = Field(gt=0, le=1_000_000, strict=True)


class DetailsReturnIn(DetailsDecisionIn):
    note: Annotated[str, BeforeValidator(lambda v: _plain(v, "note", 200))]
