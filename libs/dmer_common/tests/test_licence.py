"""Unit tests for BC driver's licence normalization (7 or 8 digits, canonical 8)."""

from __future__ import annotations

import pytest
from dmer_common.licence import normalize_licence


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("01234567", "01234567"),  # 8-digit (issued since July 2023)
        ("1234567", "01234567"),  # 7-digit (older) -> zero-padded
        ("12345678", "12345678"),
        ("0123 4567", "01234567"),  # OCR/keyed separators stripped
        (" 0123-4567 ", "01234567"),
        ("123.4567", "01234567"),
    ],
)
def test_valid_licences_normalize_to_8_digits(raw, expected):
    assert normalize_licence(raw) == expected


def test_seven_and_padded_eight_are_the_same_driver():
    # GIVEN an older 7-digit number and its backend zero-padded form
    # THEN both normalize to the same canonical value
    assert normalize_licence("1234567") == normalize_licence("01234567")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "123456",  # too short
        "123456789",  # too long
        "O1234567",  # letter O is not coerced to 0
        "0123456l",  # letter l is not coerced to 1
        "DL01234567",
        "0123#4567",  # unexpected symbol
    ],
)
def test_invalid_licences_are_rejected(raw):
    assert normalize_licence(raw) is None
