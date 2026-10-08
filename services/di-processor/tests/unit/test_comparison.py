"""Unit tests for the duplicate-comparison fields and hash."""

from __future__ import annotations

import hashlib
import json

import pytest
from di_processor.extraction.comparison import (
    CHECKBOX_FIELDS,
    COMPARISON_VERSION,
    DATE_FIELDS,
    canonical_date,
    comparison_fields,
    comparison_hash,
)
from di_processor.extraction.schemas import TopLevelExtraction, TopLevelField


def _top(**values: str) -> TopLevelExtraction:
    """A custom-model result with the given field values (confidence 0.9)."""
    return TopLevelExtraction(
        fields={k: TopLevelField(value=v, confidence=0.9) for k, v in values.items()}
    )


def _form(**overrides: str) -> TopLevelExtraction:
    """A typical form: two dates, every checkbox unselected, two selected."""
    values = {key: "unselected" for key in CHECKBOX_FIELDS}
    values.update(
        {
            "medical_examination_date": "July 3 2025",
            "medical_issued_date": "27MAY2025",
            "opinion.yes": "selected",
            "visual_field.normal": "selected",
            "details_of_condition": "FAKE handwriting",
            "dl_number": "01234567",
        }
    )
    values.update(overrides)
    return _top(**values)


def _hash(top: TopLevelExtraction) -> str:
    return comparison_hash(comparison_fields(top))


# --- field set ---------------------------------------------------------------


def test_checkbox_fields_are_unique_and_sorted():
    assert len(CHECKBOX_FIELDS) == len(set(CHECKBOX_FIELDS)) == 107
    assert list(CHECKBOX_FIELDS) == sorted(CHECKBOX_FIELDS)
    assert not set(CHECKBOX_FIELDS) & set(DATE_FIELDS)


def test_fields_always_have_every_key():
    fields = comparison_fields(_top())  # nothing returned at all
    assert set(fields) == {"version", *DATE_FIELDS, *CHECKBOX_FIELDS}
    assert fields["version"] == COMPARISON_VERSION
    assert all(fields[k] is None for k in DATE_FIELDS)
    assert not any(fields[k] for k in CHECKBOX_FIELDS)


# --- dates ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("July 3 2025", "2025-07-03"),
        ("Jul 3, 2025", "2025-07-03"),
        ("  JULY   03 2025 ", "2025-07-03"),
        ("3 July 2025", "2025-07-03"),
        ("27MAY2025", "2025-05-27"),
        ("27-May-2025", "2025-05-27"),
        ("07/03/2025", "07032025"),  # day/month order unknown: kept as text
        ("Jouy 3/2025", "jouy32025"),  # misread month: kept as text
        ("", None),
        (" / ", None),
    ],
)
def test_canonical_date(raw, expected):
    assert canonical_date(raw) == expected


# --- same form, scanned twice --------------------------------------------------


def test_same_form_with_harmless_differences_has_the_same_hash():
    first = _form()
    second = _form(
        medical_examination_date="Jul 3, 2025",  # date written/read differently
        **{"opinion.yes": " Selected "},  # case and spacing
        details_of_condition="different FAKE reading",  # not compared
        dl_number="01234568",  # not compared
    )
    # confidence is ignored
    second.fields["visual_field.normal"].confidence = 0.4
    assert _hash(first) == _hash(second)


def test_missing_checkbox_counts_as_unselected():
    first = _form()
    second = _form()
    del second.fields["sleep.cpap"]
    assert _hash(first) == _hash(second)


def test_checkbox_not_located_counts_as_unselected():
    # A box the model does not locate comes back as "" instead of "unselected"
    first = _form()
    second = _form(**{"hearing.no_hearing_aid": ""})
    assert _hash(first) == _hash(second)


# --- different forms -----------------------------------------------------------


def test_one_different_checkbox_changes_the_hash_and_is_visible():
    first = _form()
    second = _form(**{"opinion.yes": "unselected", "opinion.no": "selected"})
    assert _hash(first) != _hash(second)
    a, b = comparison_fields(first), comparison_fields(second)
    assert {k for k in a if a[k] != b[k]} == {"opinion.yes", "opinion.no"}


def test_different_exam_date_changes_the_hash():
    assert _hash(_form()) != _hash(_form(medical_examination_date="July 4 2025"))


def test_different_issued_date_changes_the_hash():
    assert _hash(_form()) != _hash(_form(medical_issued_date="02JUN2025"))


# --- hash ----------------------------------------------------------------------


def test_hash_is_sha256_of_the_stored_fields():
    fields = comparison_fields(_form())
    digest = comparison_hash(fields)
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    assert digest == hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert len(digest) == 64


def test_hash_does_not_depend_on_key_order():
    fields = comparison_fields(_form())
    reordered = dict(reversed(list(fields.items())))
    assert comparison_hash(fields) == comparison_hash(reordered)
