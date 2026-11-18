"""Unit tests for licence-number normalization (pure, no database)."""

from __future__ import annotations

from dmer_common.db.driver import normalize_licence_number


def test_normalize_uses_the_canonical_bc_form():
    # BC licences are 7 or 8 digits; 7-digit numbers are zero-padded to 8 so
    # Ingest's driver.licence_number matches Extraction's licence_number_read
    assert normalize_licence_number("1234567") == "01234567"
    assert normalize_licence_number("01234567") == "01234567"


def test_normalize_strips_separators():
    assert normalize_licence_number("0123-4567") == "01234567"
    assert normalize_licence_number(" 0123 4567 ") == "01234567"


def test_normalize_is_idempotent():
    once = normalize_licence_number("123 4567")
    assert normalize_licence_number(once) == once


def test_normalize_rejects_a_non_bc_licence():
    # a BC driver always has a BC licence number: anything else is bad data
    import pytest

    with pytest.raises(ValueError, match="BC"):
        normalize_licence_number("abc123")
