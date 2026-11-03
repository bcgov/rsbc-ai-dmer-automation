"""Unit tests for reading the licence number from the custom-model output."""

from __future__ import annotations

from di_processor.extraction.licence import read_licence
from di_processor.extraction.schemas import TopLevelExtraction, TopLevelField


def _top(value: str, confidence: float | None) -> TopLevelExtraction:
    return TopLevelExtraction(
        fields={"dl_number": TopLevelField(value=value, confidence=confidence)}
    )


def test_real_sample_read_is_kept():
    # GIVEN the sample's dl_number as the custom model read it
    assert read_licence(_top("01234567", 0.979)) == "01234567"


def test_seven_digit_read_is_zero_padded():
    assert read_licence(_top("1234567", 0.95)) == "01234567"


def test_confidence_does_not_gate_a_valid_read():
    # GIVEN a valid licence read at low or missing confidence
    # THEN it is still recorded (confidence stays in the blobs, not a gate)
    assert read_licence(_top("01234567", 0.30)) == "01234567"
    assert read_licence(_top("01234567", None)) == "01234567"


def test_invalid_read_is_null():
    # GIVEN an OCR read containing a letter THEN it is not guessed at
    assert read_licence(_top("O1234567", 0.99)) is None


def test_absent_or_blank_field_is_null():
    # absent entirely (e.g. top of the page cut off)
    assert read_licence(TopLevelExtraction()) is None
    # present but empty (DI still reports a confidence for an empty field)
    assert read_licence(_top("", 0.98)) is None
