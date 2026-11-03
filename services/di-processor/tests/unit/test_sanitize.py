"""Unit tests for LLM JSON parsing/repair and sanitize safeguards (Req 6.2, 6.5)."""

from __future__ import annotations

from di_processor.extraction.sanitize import (
    check_label_anchors,
    load_field_keys,
    load_known_form_labels,
    parse_llm_json,
    repair_json_text,
    sanitize_fields,
)
from di_processor.extraction.schemas import validate_llm_output


def test_load_field_keys_has_65_keys():
    # 67 originally; cardiovascular.congestive_heart_failure and
    # ..._has_concerns were removed -- both are checkbox-shaped fields that
    # belong to the custom DI model's schema (Stage A), not this
    # free-text/handwritten-value list. See llm_prompt_schema.md's note on
    # why no field here is a checkbox.
    keys = load_field_keys()
    assert len(keys) == 65
    assert "endocrine.HbA1C" in keys
    assert "cardiovascular.congestive_heart_failure" not in keys
    assert "cardiovascular.congestive_heart_failure_has_concerns" not in keys
    assert "cardiovascular.congestive_heart_failure_cause" in keys


def test_parse_llm_json_strips_markdown_fences():
    text = '```json\n{"fields": {}}\n```'
    assert parse_llm_json(text) == {"fields": {}}


def test_repair_json_text_collapses_ternary():
    bad = '{"x": "both" == "both" ? "high" : "low"}'
    repaired = repair_json_text(bad)
    assert '"high"' in repaired and "?" not in repaired


def test_parse_llm_json_repairs_ternary_value():
    text = '{"confidence": x ? "high" : "low"}'
    assert parse_llm_json(text) == {"confidence": "high"}


def test_sanitize_blanks_printed_label_values():
    labels = load_known_form_labels()
    # "Pacemaker" is a printed label; a value equal to it must be blanked
    result = {
        "fields": {"x": {"value": "Pacemaker", "confidence": "high", "source": "both"}}
    }
    sanitize_fields(result, ("x",), labels)
    assert result["fields"]["x"]["value"] == ""
    assert result["fields"]["x"]["confidence"] == "low"


def test_sanitize_forces_low_when_source_not_both():
    result = {"fields": {"x": {"value": "6.5", "confidence": "high", "source": "ocr"}}}
    sanitize_fields(result, ("x",), frozenset())
    assert result["fields"]["x"]["confidence"] == "low"
    # ...but the reading is kept, with a note saying why it is low
    assert result["fields"]["x"]["value"] == "6.5"
    assert result["fields"]["x"]["notes"].startswith("Confidence set to low")


def test_sanitize_keeps_low_confidence_values():
    result = {
        "fields": {
            "x": {
                "value": "FAKE-reading",
                "confidence": "low",
                "source": "image",
                "notes": "faint",
            }
        }
    }
    sanitize_fields(result, ("x",), frozenset())
    entry = result["fields"]["x"]
    assert entry["value"] == "FAKE-reading"
    assert entry["confidence"] == "low"
    assert entry["source"] == "image"
    assert entry["notes"] == "faint"


def test_sanitize_keeps_high_confidence_values_where_image_and_ocr_agree():
    result = {
        "fields": {"x": {"value": "FAKE-6.5", "confidence": "high", "source": "both"}}
    }
    sanitize_fields(result, ("x",), frozenset())
    assert result["fields"]["x"]["value"] == "FAKE-6.5"
    assert result["fields"]["x"]["confidence"] == "high"


def test_sanitize_fills_missing_keys():
    result = {"fields": {}}
    sanitize_fields(result, ("a", "b"), frozenset())
    assert set(result["fields"]) == {"a", "b"}
    assert result["fields"]["a"]["source"] == "none"


def test_sanitize_handles_missing_fields_object():
    result = {}
    sanitize_fields(result, ("a",), frozenset())
    assert "a" in result["fields"]


def test_sanitize_forces_low_on_blank_high_confidence_field():
    # The first real dev run failed here: GPT marks a field it is sure is EMPTY
    # as high confidence with source "none", which the schema rejects -- and a
    # real reply has dozens of such fields.
    result = {"fields": {"x": {"value": "", "confidence": "high", "source": "none"}}}
    sanitize_fields(result, ("x",), frozenset())
    assert result["fields"]["x"]["confidence"] == "low"
    assert result["fields"]["x"]["notes"] == ""  # blank fields need no note
    validate_llm_output(result)  # must now pass the schema


def test_sanitize_normalizes_off_schema_types():
    result = {
        "fields": {
            "a": {"value": None, "confidence": "medium", "source": "BOTH"},
            "b": {"value": 6.5, "confidence": "High", "source": "both", "notes": None},
            "c": {"value": "x", "confidence": "high", "source": "handwriting"},
        }
    }
    sanitize_fields(result, ("a", "b", "c"), frozenset())
    f = result["fields"]
    assert f["a"] == {"value": "", "confidence": "low", "source": "both", "notes": ""}
    assert f["b"]["value"] == "6.5" and f["b"]["confidence"] == "high"
    assert f["c"]["source"] == "none" and f["c"]["confidence"] == "low"
    validate_llm_output(result)


def test_realistic_reply_with_many_blank_fields_validates():
    # Shape of a real reply: every key present, most blank-but-"high".
    keys = load_field_keys()
    reply = {
        "fields": {
            k: {"value": "", "confidence": "high", "source": "none", "notes": ""}
            for k in keys
        },
        "uncertain_fields": [],
    }
    reply["fields"][keys[0]] = {
        "value": "FAKE-7.2",
        "confidence": "high",
        "source": "both",
        "notes": "",
    }
    sanitize_fields(reply, keys, frozenset())
    result = validate_llm_output(reply)
    assert result.fields[keys[0]].confidence.value == "high"  # agreed value kept
    assert all(
        f.confidence.value == "low" for k, f in result.fields.items() if k != keys[0]
    )


def _ocr(*segment_texts):
    return {"pages": [{"rows": [{"segments": [{"text": t} for t in segment_texts]}]}]}


def _moca(value, source="both", confidence="high"):
    return {
        "fields": {
            "cns.moca_score": {
                "value": value,
                "confidence": confidence,
                "source": source,
                "notes": "x",
            }
        }
    }


def test_anchor_blanks_moca_taken_from_another_section():
    # DMER21: "29/30" is the OCR of a visual acuity, not next to MOCA.
    ocr = _ocr("Cognitive Impairment MOCA\nTrails B", "L\nBoth\n29/30\nNormal")
    entry = check_label_anchors(_moca("29/30"), ocr)["fields"]["cns.moca_score"]
    assert entry["value"] == ""
    assert entry["confidence"] == "low"
    assert "away from the MOCA label" in entry["notes"]


def test_anchor_keeps_moca_written_next_to_its_label():
    ocr = _ocr("Cognitive Impairment MOCA 24/30\nTrails B")
    entry = check_label_anchors(_moca("24 / 30"), ocr)["fields"]["cns.moca_score"]
    assert entry["value"] == "24 / 30"
    assert entry["confidence"] == "high"


def test_anchor_keeps_moca_the_ocr_does_not_show():
    # An image-only reading: nothing in the OCR to contradict it.
    ocr = _ocr("Cognitive Impairment MOCA\nTrails B")
    result = check_label_anchors(_moca("26/30", "image", "low"), ocr)
    assert result["fields"]["cns.moca_score"]["value"] == "26/30"


def test_anchor_ignores_blank_and_missing_fields():
    assert check_label_anchors({"fields": {}}, _ocr("29/30"))["fields"] == {}
    entry = check_label_anchors(_moca(""), _ocr("29/30"))["fields"]["cns.moca_score"]
    assert entry["value"] == ""
