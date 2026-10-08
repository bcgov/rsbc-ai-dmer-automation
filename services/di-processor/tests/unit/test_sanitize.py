"""Unit tests for LLM JSON parsing/repair and sanitize safeguards (Req 6.2, 6.5)."""

from __future__ import annotations

from di_processor.extraction.sanitize import (
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
    # ...and a low-confidence reading is withheld
    assert result["fields"]["x"]["value"] == ""


def test_sanitize_blanks_low_confidence_values_with_a_note():
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
    assert entry["value"] == ""
    assert entry["confidence"] == "low"
    assert entry["source"] == "image"  # where the withheld reading came from
    assert entry["notes"].startswith("faint | Value withheld: low confidence")


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
