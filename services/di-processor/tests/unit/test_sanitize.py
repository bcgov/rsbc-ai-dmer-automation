"""Unit tests for LLM JSON parsing/repair and sanitize safeguards (Req 6.2, 6.5)."""

from __future__ import annotations

from di_processor.extraction.sanitize import (
    load_field_keys,
    load_known_form_labels,
    parse_llm_json,
    repair_json_text,
    sanitize_fields,
)


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


def test_sanitize_fills_missing_keys():
    result = {"fields": {}}
    sanitize_fields(result, ("a", "b"), frozenset())
    assert set(result["fields"]) == {"a", "b"}
    assert result["fields"]["a"]["source"] == "none"


def test_sanitize_handles_missing_fields_object():
    result = {}
    sanitize_fields(result, ("a",), frozenset())
    assert "a" in result["fields"]
