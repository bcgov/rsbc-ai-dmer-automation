"""Unit tests for extraction schemas and validate_llm_output.

Behaviour specs (GIVEN/WHEN/THEN) for the binary-confidence rule, malformed LLM
output rejection, and the combined extraction shape (Requirements 6.2, 6.5).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from di_processor.extraction.schemas import (
    CombinedExtraction,
    Confidence,
    ExtractionError,
    HandwrittenExtraction,
    HandwrittenField,
    Source,
    TopLevelExtraction,
    validate_llm_output,
)


def test_high_confidence_requires_source_both():
    # GIVEN a field claiming high confidence but sources did not both agree
    # WHEN constructed THEN it is rejected (binary-confidence rule)
    with pytest.raises(ValueError):
        HandwrittenField(value="6.5", confidence=Confidence.HIGH, source=Source.OCR)


def test_high_confidence_allowed_when_source_both():
    # GIVEN image and OCR agree
    field = HandwrittenField(
        value="6.5", confidence=Confidence.HIGH, source=Source.BOTH, notes="agree"
    )
    # THEN high confidence is accepted
    assert field.confidence is Confidence.HIGH
    assert field.source is Source.BOTH


def test_low_confidence_with_any_source_ok():
    # GIVEN a low-confidence field from OCR only
    field = HandwrittenField(
        value="Dec 2023", confidence=Confidence.LOW, source=Source.OCR
    )
    # THEN it is valid
    assert field.confidence is Confidence.LOW


def test_validate_llm_output_parses_valid_payload():
    # GIVEN a well-formed LLM payload
    raw = {
        "fields": {
            "endocrine.HbA1C": {
                "value": "6.5",
                "confidence": "high",
                "source": "both",
                "notes": "image and OCR agree",
            },
            "cardiovascular.arrhythmia_type": {
                "value": "",
                "confidence": "low",
                "source": "none",
                "notes": "blank field",
            },
        },
        "uncertain_fields": ["cardiovascular.arrhythmia_type"],
    }
    # WHEN validated
    result = validate_llm_output(raw)
    # THEN it parses into a HandwrittenExtraction
    assert isinstance(result, HandwrittenExtraction)
    assert result.fields["endocrine.HbA1C"].value == "6.5"
    assert result.uncertain_fields == ["cardiovascular.arrhythmia_type"]


def test_validate_llm_output_rejects_medium_confidence():
    # GIVEN an LLM payload using the forbidden 'medium' confidence
    raw = {"fields": {"x": {"value": "a", "confidence": "medium", "source": "ocr"}}}
    # WHEN validated THEN it raises ExtractionError (Req 6.5)
    with pytest.raises(ExtractionError):
        validate_llm_output(raw)


def test_validate_llm_output_rejects_binary_confidence_violation():
    # GIVEN high confidence without source 'both'
    raw = {"fields": {"x": {"value": "a", "confidence": "high", "source": "image"}}}
    # WHEN validated THEN it raises ExtractionError
    with pytest.raises(ExtractionError):
        validate_llm_output(raw)


def test_validate_llm_output_rejects_unknown_field_keys_in_entry():
    # GIVEN an entry with an unexpected attribute
    raw = {"fields": {"x": {"value": "a", "source": "none", "unexpected": 1}}}
    # WHEN validated THEN it raises (extra=forbid guards drift)
    with pytest.raises(ExtractionError):
        validate_llm_output(raw)


def test_top_level_extraction_parses():
    # GIVEN custom-model output
    top = TopLevelExtraction.model_validate(
        {"fields": {"physician.name": {"value": "Dr X", "confidence": 0.98}}}
    )
    # THEN it normalizes into TopLevelField entries
    assert top.fields["physician.name"].value == "Dr X"
    assert top.fields["physician.name"].confidence == 0.98


def test_combined_extraction_shape():
    # GIVEN a unified combined extraction
    combined = CombinedExtraction(
        document_id="doc-1",
        processed_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
        fields={"top_level.physician.name": "Dr X", "endocrine.HbA1C": "6.5"},
        uncertain_fields=[],
    )
    # THEN metadata and the flat field map are present
    assert combined.document_id == "doc-1"
    assert combined.fields["top_level.physician.name"] == "Dr X"
