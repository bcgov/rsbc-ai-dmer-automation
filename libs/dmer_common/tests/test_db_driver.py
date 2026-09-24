"""Unit tests for licence-number normalization (pure, no database)."""

from __future__ import annotations

from dmer_common.db.driver import normalize_licence_number


def test_normalize_uppercases():
    assert normalize_licence_number("abc123") == "ABC123"


def test_normalize_strips_punctuation_and_whitespace():
    # pragma: allowlist nextline secret -- test fixture, not a real credential
    assert normalize_licence_number("abc-123 456") == "ABC123456"


def test_normalize_is_idempotent():
    once = normalize_licence_number("A-1 2b")
    assert normalize_licence_number(once) == once
