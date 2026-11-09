"""Tests for dmer_common.triage.policy_text: the procedures PDF as text.

The PDF itself isn't in the repository. Tests that need it read the path from
DMER_TRIAGE_POLICY_PDF and are skipped without it:

    DMER_TRIAGE_POLICY_PDF=/path/to/Triage_Sort_Procedures.pdf pytest tests/test_triage_policy_text.py
"""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path

import pytest
from dmer_common.triage import PolicyTextError, build, convert, updated_date, verify

pytest.importorskip("pdfplumber")

_PDF = os.environ.get("DMER_TRIAGE_POLICY_PDF")
needs_pdf = pytest.mark.skipif(
    not (_PDF and Path(_PDF).is_file()),
    reason="DMER_TRIAGE_POLICY_PDF not set to the procedures PDF",
)


@pytest.fixture(scope="module")
def pdf() -> bytes:
    return Path(_PDF).read_bytes()


@pytest.fixture(scope="module")
def text(pdf: bytes) -> str:
    return build(pdf)


@pytest.mark.parametrize(
    "first_line, expected",
    [
        (
            "TRIAGE SORT PROCEDURES FOR DMERS (updated 12Jun2026) - converted",
            date(2026, 6, 12),
        ),
        (
            "TRIAGE SORT PROCEDURES FOR DMERS (updated 3September2027) - converted",
            date(2027, 9, 3),
        ),
        ("TRIAGE SORT PROCEDURES FOR DMERS (updated unknown date) - converted", None),
        ("no header at all", None),
    ],
)
def test_updated_date_is_read_from_the_first_line(first_line, expected):
    assert (
        updated_date(first_line + "\nrest of the text (updated 1Jan2020)") == expected
    )


def test_a_file_that_is_not_a_pdf_is_rejected():
    with pytest.raises(PolicyTextError, match="Not a readable PDF"):
        build(b"%PDF-not really")


@needs_pdf
def test_every_page_of_the_converted_text_matches_the_pdf(pdf, text):
    assert verify(pdf, text) == []
    assert text.count("=== Page ") == text.count("\n=== Page ")  # one header per line
    assert updated_date(text) is not None


@needs_pdf
def test_rules_keep_their_class_columns_apart(text):
    blocks = text.split("- Condition: ")[1:]
    assert blocks
    for block in blocks:
        class58 = block.index("  Class 5-8:")
        high = block.index("  High Class 1-4 / Class 5 with R18, 19, 20:")
        assert class58 < high


@needs_pdf
def test_converting_twice_gives_the_same_text(pdf, text):
    assert convert(pdf) == text


def _lines(text: str) -> list[str]:
    return text.splitlines()


def _first(lines: list[str], predicate) -> int:
    return next(i for i, line in enumerate(lines) if predicate(line))


def _replace(lines, predicate, old, new):
    i = _first(lines, predicate)
    lines[i] = lines[i].replace(old, new, 1)
    return lines


def _move_rule_to_high_class(lines):
    start = _first(lines, lambda line: line.strip() == "Class 5-8:")
    high = next(
        i for i in range(start, len(lines)) if lines[i].strip().startswith("High Class")
    )
    rule = lines.pop(start + 1)
    lines.insert(high, rule)
    return lines


def _drop_last_word_of_a_condition(lines):
    i = _first(
        lines, lambda line: line.startswith("- Condition:") and len(line.split()) > 4
    )
    lines[i] = " ".join(lines[i].split()[:-1])
    return lines


def _drop_a_page(lines):
    start = _first(lines, lambda line: line.startswith("=== Page 12"))
    end = _first(lines, lambda line: line.startswith("=== Page 13"))
    return lines[:start] + lines[end:]


@needs_pdf
@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(
            lambda ls: _replace(
                ls, lambda line: line.rstrip().endswith("-> PU"), "-> PU", "-> PR"
            ),
            id="an action code changed",
        ),
        pytest.param(
            lambda ls: _replace(
                ls, lambda line: line.rstrip().endswith("-> IN*"), "-> IN*", "-> IN"
            ),
            id="an asterisk dropped",
        ),
        pytest.param(
            lambda ls: _replace(ls, lambda line: "20/50" in line, "20/50", "20/40"),
            id="a threshold changed",
        ),
        pytest.param(
            _move_rule_to_high_class, id="a rule moved to the other class column"
        ),
        pytest.param(_drop_last_word_of_a_condition, id="a word dropped"),
        pytest.param(_drop_a_page, id="a page dropped"),
    ],
)
def test_verify_catches_a_text_that_does_not_match(pdf, text, corrupt):
    corrupted = "\n".join(corrupt(_lines(text)))
    assert corrupted != text
    assert verify(pdf, corrupted)
