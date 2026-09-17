"""Unit tests for merging handwritten values onto the custom-model base (Req 6.4).

Semantics: the custom-model output is the complete, authoritative base. The
handwritten path only fills/refines values of keys that already exist, and only
when it actually captured content (never blanks a custom-model value, never adds
new keys).
"""

from __future__ import annotations

from datetime import UTC, datetime

from di_processor.extraction.merge import merge
from di_processor.extraction.schemas import (
    Confidence,
    HandwrittenExtraction,
    HandwrittenField,
    Source,
    TopLevelExtraction,
    TopLevelField,
)


def _top_level(**fields: str) -> TopLevelExtraction:
    return TopLevelExtraction(
        fields={k: TopLevelField(value=v) for k, v in fields.items()}
    )


def test_base_is_all_custom_model_fields():
    top = _top_level(**{"vision.acuity_loss": "selected", "dl_number": "01234567"})
    combined = merge("doc-1", "c", top, HandwrittenExtraction())
    # every custom-model field is present, un-namespaced
    assert combined.fields["vision.acuity_loss"] == "selected"
    assert combined.fields["dl_number"] == "01234567"


def test_handwritten_fills_blank_custom_field():
    # custom model left HbA1C blank; handwritten captured "6.5"
    top = _top_level(**{"endocrine.HbA1C": ""})
    hw = HandwrittenExtraction(
        fields={
            "endocrine.HbA1C": HandwrittenField(
                value="6.5", confidence=Confidence.HIGH, source=Source.BOTH
            )
        }
    )
    combined = merge("doc-1", "c", top, hw)
    assert combined.fields["endocrine.HbA1C"] == "6.5"


def test_blank_handwritten_does_not_overwrite_custom_value():
    # custom model read "cane-use"; handwritten found it blank (source none)
    top = _top_level(**{"musculoskeletal.weakness_details": "cane-use"})
    hw = HandwrittenExtraction(
        fields={
            "musculoskeletal.weakness_details": HandwrittenField(
                value="", confidence=Confidence.LOW, source=Source.NONE
            )
        }
    )
    combined = merge("doc-1", "c", top, hw)
    # custom-model value is preserved
    assert combined.fields["musculoskeletal.weakness_details"] == "cane-use"


def test_handwritten_only_key_is_not_added():
    # handwritten produces a key the custom model does not have -> ignored
    top = _top_level(dl_number="01234567")
    hw = HandwrittenExtraction(
        fields={
            "some.unknown_key": HandwrittenField(
                value="x", confidence=Confidence.LOW, source=Source.IMAGE
            )
        }
    )
    combined = merge("doc-1", "c", top, hw)
    assert "some.unknown_key" not in combined.fields


def test_low_confidence_image_value_still_fills_existing_key():
    top = _top_level(**{"visual_acuity.uncorrected_right": ""})
    hw = HandwrittenExtraction(
        fields={
            "visual_acuity.uncorrected_right": HandwrittenField(
                value="24/30", confidence=Confidence.LOW, source=Source.IMAGE
            )
        }
    )
    combined = merge("doc-1", "c", top, hw)
    assert combined.fields["visual_acuity.uncorrected_right"] == "24/30"


def test_metadata_and_uncertain_fields_carried():
    ts = datetime(2026, 8, 5, 12, 5, tzinfo=UTC)
    top = _top_level(dl_number="1")
    hw = HandwrittenExtraction(uncertain_fields=["visual_acuity.uncorrected_right"])
    combined = merge(
        "doc-1",
        "case-123",
        top,
        hw,
        source_model_version="rsbc-ocr-dmer-v9",
        processed_at=ts,
    )
    assert combined.document_id == "doc-1"
    assert combined.source_model_version == "rsbc-ocr-dmer-v9"
    assert combined.processed_at == ts
    assert combined.uncertain_fields == ["visual_acuity.uncorrected_right"]
