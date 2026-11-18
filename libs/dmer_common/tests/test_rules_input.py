"""The pass-through field types (normalization/rules_input.py) against rules.json.

The rules' input schema rejects a whole document when one field has the wrong
type -- e.g. a checkbox DI didn't locate, written as "", where the rules expect
a boolean. These tests keep PASSTHROUGH_FIELD_TYPES in step with the ruleset and
check that what adapt_combined_fields produces for those fields is accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from dmer_common.normalization.pipeline import adapt_combined_fields
from dmer_common.normalization.rules_input import (
    DERIVED_FIELDS,
    PASSTHROUGH_FIELD_TYPES,
)
from dmer_common.normalization.schema import CONDITIONS
from dmer_common.rules import RuleEvaluationError, Ruleset

# Normalize-shaped input, built the way the rule engine's own tests build it.
from test_rules_engine import base_dmer, normalize

RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "rule-engine"
    / "rules"
    / "rules.json"
)
RULES_JSON = RULES_PATH.read_text(encoding="utf-8")

_JSON_TYPES = {"bool": "boolean", "str": "string"}


def _rules_input_properties() -> dict[str, dict]:
    rules = json.loads(RULES_JSON)
    (node,) = [n for n in rules["nodes"] if n["type"] == "inputNode"]
    schema = node["content"]["schema"]
    schema = json.loads(schema) if isinstance(schema, str) else schema
    return schema["properties"]["dmer"]["properties"]


RULES_INPUT = _rules_input_properties()


def test_every_rules_field_outside_the_schema_is_passed_through_or_derived():
    # A rules input field Normalize neither analyses (CONDITIONS), derives,
    # nor dates must be in PASSTHROUGH_FIELD_TYPES, or nothing types it.
    uncovered = sorted(
        name
        for name in RULES_INPUT
        if name not in CONDITIONS
        and name not in DERIVED_FIELDS
        and "date" not in name.lower()
        and name not in PASSTHROUGH_FIELD_TYPES
    )
    assert uncovered == []


@pytest.mark.parametrize("name,kind", sorted(PASSTHROUGH_FIELD_TYPES.items()))
def test_passthrough_type_matches_the_rules_input_schema(name, kind):
    assert name not in CONDITIONS, "a schema field is typed by the schema"
    assert name in RULES_INPUT, "not read by the rules any more"
    assert RULES_INPUT[name].get("type") == _JSON_TYPES[kind]


def test_derived_fields_are_outside_the_schema_and_read_by_the_rules():
    for name in DERIVED_FIELDS:
        assert name not in CONDITIONS
        assert name in RULES_INPUT


def test_blank_and_missing_passthrough_values_pass_the_rules_input_schema():
    # The 2026-10-05 dev failures: a checkbox DI didn't locate ("") reached
    # the rules as a string -> "is not of type boolean" -> manual review.
    raw = {
        name: ("" if kind == "bool" else None)
        for name, kind in PASSTHROUGH_FIELD_TYPES.items()
    }
    adapted = adapt_combined_fields(raw)["dmer"]
    for name, kind in PASSTHROUGH_FIELD_TYPES.items():
        expected = bool if kind == "bool" else str
        assert isinstance(adapted[name], expected), name

    ruleset = Ruleset(RULES_JSON)
    received = "2026-10-05T00:00:00+00:00"
    # A full Normalize-shaped document with those values evaluates...
    ruleset.evaluate(normalize({**base_dmer(), **adapted}), received_date=received)
    # ...where the raw "" it replaced is rejected by the input schema.
    with pytest.raises(RuleEvaluationError):
        ruleset.evaluate(
            normalize({**base_dmer(), **adapted, "hearing.no_hearing_aid": ""}),
            received_date=received,
        )
