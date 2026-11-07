"""Money values. Everything is an integer number of paise (1 rupee = 100 paise).

Floats never touch money: JSON numbers with a fractional part are refused,
strings are parsed with an ASCII-only regex (Python's int() and \\d accept
non-ASCII digits such as '١٢٣', which we do not want).
"""

from __future__ import annotations

import re

PAISE_PER_RUPEE = 100
MAX_TRANSACTION_PAISE = 10_00_000 * PAISE_PER_RUPEE   # Rs 10,00,000 per request
MAX_OVERDRAFT_PAISE = 50_000 * PAISE_PER_RUPEE        # Rs 50,000 (mirrors the DB CHECK)

_AMOUNT_RE = re.compile(r"(0|[1-9][0-9]{0,9})(?:\.([0-9]{1,2}))?", re.ASCII)


class AmountError(ValueError):
    """Raised when a value is not an acceptable money amount."""


def parse_amount(value: object, *, allow_zero: bool = False, maximum: int = MAX_TRANSACTION_PAISE) -> int:
    """Parse a client-supplied amount in rupees into paise.

    Accepted: a string like "1500" / "1500.5" / "1500.50", or a JSON integer (rupees).
    Refused: floats, booleans, negatives, more than 2 decimals, exponents,
    non-ASCII digits, whitespace tricks, zero, and anything above ``maximum``.
    """
    if isinstance(value, bool):                      # bool is a subclass of int
        raise AmountError("amount must be a number of rupees, not a boolean")
    if isinstance(value, int):
        if value < 0:
            raise AmountError("amount must be positive")
        paise = value * PAISE_PER_RUPEE
    elif isinstance(value, str):
        match = _AMOUNT_RE.fullmatch(value)
        if not match:
            raise AmountError('amount must look like "1500" or "1500.50" (rupees, at most 2 decimals)')
        whole, frac = match.group(1), match.group(2) or ""
        paise = int(whole) * PAISE_PER_RUPEE + int(frac.ljust(2, "0") or "0")
    else:
        raise AmountError('send the amount as a string such as "1500.50" (floats are not accepted for money)')
    if paise == 0 and not allow_zero:
        raise AmountError("amount must be greater than zero")
    if paise > maximum:
        raise AmountError(f"amount exceeds the per-request maximum of Rs {format_paise(maximum)}")
    return paise


def format_paise(paise: int) -> str:
    """Plain machine format: 123456 -> '1234.56' (negative keeps its sign)."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    return f"{sign}{paise // PAISE_PER_RUPEE}.{paise % PAISE_PER_RUPEE:02d}"


def format_inr(paise: int) -> str:
    """Human format with Indian digit grouping: 12345678 -> 'Rs 1,23,456.78'."""
    sign = "-" if paise < 0 else ""
    paise = abs(paise)
    rupees, rem = divmod(paise, PAISE_PER_RUPEE)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{sign}Rs {digits}.{rem:02d}"


def money_view(paise: int | None) -> dict | None:
    if paise is None:
        return None
    return {"paise": paise, "rupees": format_paise(paise), "display": format_inr(paise)}
