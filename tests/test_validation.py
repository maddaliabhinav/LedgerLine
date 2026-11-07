"""Money parsing and text validation (no database)."""

from __future__ import annotations

import pytest

from app.api.schemas import _memo
from app.money import AmountError, format_inr, parse_amount


@pytest.mark.parametrize("value, paise", [
    ("1500", 150000), ("1500.5", 150050), ("1500.50", 150050), ("0.01", 1), (25, 2500), ("1000000", 100000000),
])
def test_valid_amounts(value, paise):
    assert parse_amount(value) == paise


@pytest.mark.parametrize("value", [
    -1, 0, "0", "0.00", 1.5, 2.0, True, False, "1e3", "1,000", " 100", "100 ", "١٢٣", "１２３", "1.234", "+5", "-5",
    "", None, [], {}, "1000000.01", 1000001, "0x10", "1_000", "NaN", "Infinity",
])
def test_invalid_amounts(value):
    with pytest.raises(AmountError):
        parse_amount(value)


def test_indian_grouping():
    assert format_inr(12345678) == "Rs 1,23,456.78"
    assert format_inr(0) == "Rs 0.00"
    assert format_inr(-150) == "-Rs 1.50"
    assert format_inr(100000000) == "Rs 10,00,000.00"


@pytest.mark.parametrize("bad", ["a\x00b", "line\nbreak", "evil‮exe.txt", "x" * 141, "⁦hidden⁩"])
def test_memo_rejects_control_and_direction_characters(bad):
    with pytest.raises(ValueError):
        _memo(bad)


def test_memo_accepts_normal_text():
    assert _memo("  Dinner at Café Udupi — ₹ split  ") == "Dinner at Café Udupi — ₹ split"
    assert _memo("") is None and _memo(None) is None
