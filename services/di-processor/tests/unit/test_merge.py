"""Unit tests for merging handwritten values onto the custom-model base (Req 6.4).

Semantics: the custom-model output defines the keys. For every handwritten key
it also output: a high-confidence handwritten value wins; a low-confidence one
wins unless the custom model read the field with confidence above 0.75, in which
case the custom-model value is kept. The handwritten value used can be empty.
A visual acuity field the custom model read with confidence 0.95 or above keeps
the custom-model value (empty included), whatever the handwritten reading.
Handwritten keys the custom model did not output are never added.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from di_processor.extraction.merge import (
    CUSTOM_MODEL_KEEP_CONFIDENCE,
    VISUAL_ACUITY_FIELDS,
    VISUAL_ACUITY_KEEP_CONFIDENCE,
    merge,
)
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


def _one(custom_value, custom_conf, hw_value, hw_conf, hw_source=Source.IMAGE):
    """Merge one field: custom (value, confidence) vs handwritten (value, conf)."""
    top = TopLevelExtraction(
        fields={
            "cns.moca_score": TopLevelField(value=custom_value, confidence=custom_conf)
        }
    )
    if hw_conf is Confidence.HIGH:
        hw_source = Source.BOTH  # the schema allows high only with source both
    hw = HandwrittenExtraction(
        fields={
            "cns.moca_score": HandwrittenField(
                value=hw_value, confidence=hw_conf, source=hw_source
            )
        }
    )
    return merge("doc-1", top, hw).fields["cns.moca_score"]


def test_threshold_is_075():
    assert CUSTOM_MODEL_KEEP_CONFIDENCE == 0.75


def test_high_handwritten_wins_even_over_a_confident_custom_value():
    assert _one("24/30", 0.99, "26/30", Confidence.HIGH) == "26/30"


def test_low_handwritten_does_not_replace_a_confident_custom_value():
    assert _one("24/30", 0.9, "26/30", Confidence.LOW) == "24/30"


def test_low_blank_handwritten_does_not_blank_a_confident_custom_value():
    assert _one("24/30", 0.76, "", Confidence.LOW, Source.NONE) == "24/30"


@pytest.mark.parametrize("custom_conf", [0.75, 0.5, 0.0, None])
def test_low_handwritten_replaces_a_custom_value_at_or_below_threshold(custom_conf):
    # exactly 0.75 is not "above", and a missing confidence counts as not confident
    assert _one("24/30", custom_conf, "26/30", Confidence.LOW) == "26/30"


def test_low_blank_handwritten_blanks_an_unconfident_custom_value():
    assert _one("24/30", 0.6, "", Confidence.LOW, Source.NONE) == ""


def test_base_is_all_custom_model_fields():
    top = _top_level(**{"vision.acuity_loss": "selected", "dl_number": "01234567"})
    combined = merge("doc-1", top, HandwrittenExtraction())
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
    combined = merge("doc-1", top, hw)
    assert combined.fields["endocrine.HbA1C"] == "6.5"


def test_handwritten_value_replaces_custom_value():
    # custom model read "6.2"; handwritten read "6.5" -> handwritten wins
    top = _top_level(**{"endocrine.HbA1C": "6.2"})
    hw = HandwrittenExtraction(
        fields={
            "endocrine.HbA1C": HandwrittenField(
                value="6.5", confidence=Confidence.HIGH, source=Source.BOTH
            )
        }
    )
    combined = merge("doc-1", top, hw)
    assert combined.fields["endocrine.HbA1C"] == "6.5"


def test_blank_handwritten_overwrites_custom_value():
    # custom model read "cane-use"; handwritten found it blank (source none)
    top = _top_level(**{"musculoskeletal.weakness_details": "cane-use"})
    hw = HandwrittenExtraction(
        fields={
            "musculoskeletal.weakness_details": HandwrittenField(
                value="", confidence=Confidence.LOW, source=Source.NONE
            )
        }
    )
    combined = merge("doc-1", top, hw)
    # the handwritten (blank) value wins
    assert combined.fields["musculoskeletal.weakness_details"] == ""


def test_custom_fields_without_a_handwritten_reading_are_kept():
    # checkboxes / identity fields are not handwritten keys -> pass through
    top = _top_level(**{"vision.acuity_loss": "true", "endocrine.HbA1C": "6.2"})
    hw = HandwrittenExtraction(
        fields={
            "endocrine.HbA1C": HandwrittenField(
                value="", confidence=Confidence.LOW, source=Source.NONE
            )
        }
    )
    combined = merge("doc-1", top, hw)
    assert combined.fields["vision.acuity_loss"] == "true"
    assert combined.fields["endocrine.HbA1C"] == ""


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
    combined = merge("doc-1", top, hw)
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
    combined = merge("doc-1", top, hw)
    assert combined.fields["visual_acuity.uncorrected_right"] == "24/30"


def _acuity(field, custom_value, custom_conf, hw_value, hw_conf):
    """Merge one visual acuity field: custom (value, conf) vs handwritten."""
    top = TopLevelExtraction(
        fields={field: TopLevelField(value=custom_value, confidence=custom_conf)}
    )
    source = Source.BOTH if hw_conf is Confidence.HIGH else Source.IMAGE
    hw = HandwrittenExtraction(
        fields={
            field: HandwrittenField(value=hw_value, confidence=hw_conf, source=source)
        }
    )
    return merge("doc-1", top, hw).fields[field]


def test_visual_acuity_threshold_and_fields():
    assert VISUAL_ACUITY_KEEP_CONFIDENCE == 0.95
    assert VISUAL_ACUITY_FIELDS == {
        "visual_acuity.corrected_left",
        "visual_acuity.corrected_right",
        "visual_acuity.corrected_both",
        "visual_acuity.uncorrected_left",
        "visual_acuity.uncorrected_right",
        "visual_acuity.uncorrected_both",
    }


@pytest.mark.parametrize("field", sorted(VISUAL_ACUITY_FIELDS))
@pytest.mark.parametrize("custom_conf", [0.95, 0.99])
def test_confident_custom_acuity_value_beats_high_handwritten(field, custom_conf):
    assert _acuity(field, "20/30", custom_conf, "20/80", Confidence.HIGH) == "20/30"


@pytest.mark.parametrize("field", sorted(VISUAL_ACUITY_FIELDS))
def test_confident_custom_acuity_blank_beats_high_handwritten(field):
    # the custom model is sure the box is empty -> it stays empty
    assert _acuity(field, "", 0.98, "20/40", Confidence.HIGH) == ""


def test_confident_custom_acuity_value_is_not_blanked_by_handwritten():
    field = "visual_acuity.corrected_both"
    assert _acuity(field, "20/30", 0.96, "", Confidence.LOW) == "20/30"


@pytest.mark.parametrize(
    ("custom_conf", "hw_conf", "expected"),
    [
        (0.949, Confidence.HIGH, "20/80"),  # below 0.95: high handwritten wins
        (0.9, Confidence.LOW, "20/30"),  # above 0.75: low handwritten loses
        (0.7, Confidence.LOW, "20/80"),  # 0.75 or below: low handwritten wins
        (None, Confidence.HIGH, "20/80"),  # no confidence: existing rule
    ],
)
def test_acuity_below_095_follows_the_existing_rule(custom_conf, hw_conf, expected):
    field = "visual_acuity.corrected_right"
    assert _acuity(field, "20/30", custom_conf, "20/80", hw_conf) == expected


def test_095_rule_applies_only_to_visual_acuity():
    # a non-acuity field at 0.99 still loses to a high handwritten reading
    assert _one("", 0.99, "26/30", Confidence.HIGH) == "26/30"


def test_metadata_and_uncertain_fields_carried():
    ts = datetime(2026, 8, 5, 12, 5, tzinfo=UTC)
    top = _top_level(dl_number="1")
    hw = HandwrittenExtraction(uncertain_fields=["visual_acuity.uncorrected_right"])
    combined = merge(
        "doc-1",
        top,
        hw,
        source_model_version="rsbc-ocr-dmer-v9",
        processed_at=ts,
    )
    assert combined.document_id == "doc-1"
    assert combined.source_model_version == "rsbc-ocr-dmer-v9"
    assert combined.processed_at == ts
    assert combined.uncertain_fields == ["visual_acuity.uncorrected_right"]
