"""Unit tests for dmer_common.normalization's deterministic (non-LLM) logic:
the input adapter, NYHA/MMSE-MoCA/restrictions parsers, the OCR-digit-
confusion date fallback, the alcohol-withdrawal-seizure conflict rule,
no_other_conditions, guide_with_no_matching_condition, and the output
validators (schema + evidence presence).

Pure functions (or functions operating on an already-built dict) -- no
Azure OpenAI call is made, so this file needs no credentials and always
runs (ported from the llm_normalization POC's
test_deterministic_normalization.py, plus new tests for the pieces added
on port: adapt_combined_fields, validate_schema, validate_evidence_present).
"""

from __future__ import annotations

import pytest
from dmer_common.normalization.pipeline import (
    NormalizationValidationError,
    adapt_combined_fields,
    check_diabetes_guide_9_1,
    check_diabetes_treatment_not_indicated,
    check_guide_matching,
    check_no_other_conditions,
    normalize_dates,
    normalize_restrictions,
    parse_cognitive_score,
    parse_guide_section,
    parse_guide_subsection,
    parse_nyha_class,
    resolve_conflicts,
    validate_evidence_present,
    validate_schema,
)

# ---------------------------------------------------------------------------
# adapt_combined_fields -- di-processor's real CombinedExtraction shape
# ---------------------------------------------------------------------------

def test_adapt_combined_fields_coerces_checkbox_strings_to_bool():
    adapted = adapt_combined_fields({"vision.cataracts": "true", "vestibular.drop_attacks": "false"})
    assert adapted["dmer"]["vision.cataracts"] is True
    assert adapted["dmer"]["vestibular.drop_attacks"] is False


def test_adapt_combined_fields_passes_through_non_bool_fields_unchanged():
    adapted = adapt_combined_fields({"cardiovascular.nyha_class": "3", "vision.other": "some text"})
    assert adapted["dmer"]["cardiovascular.nyha_class"] == "3"
    assert adapted["dmer"]["vision.other"] == "some text"


def test_adapt_combined_fields_passes_through_unknown_fields():
    # GIVEN a field not in the CONDITIONS schema (e.g. "guide", or a future
    # DI model field) THEN it passes through unchanged rather than erroring
    adapted = adapt_combined_fields({"guide": "9.1", "some_new_field": "value"})
    assert adapted["dmer"]["guide"] == "9.1"
    assert adapted["dmer"]["some_new_field"] == "value"


def test_adapt_combined_fields_accepts_real_bool_too():
    # Not every caller will hand this a string -- a real bool must pass through as-is
    adapted = adapt_combined_fields({"vision.cataracts": True})
    assert adapted["dmer"]["vision.cataracts"] is True


# ---------------------------------------------------------------------------
# NYHA class
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("III", 3), ("iv", 4), ("3", 3), (3, 3), ("", None), (None, None), ("V", None), (5, None), (0, None)],
)
def test_parse_nyha_class(raw, expected):
    assert parse_nyha_class(raw) == expected


# ---------------------------------------------------------------------------
# MMSE / MoCA
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [("26/30", 26), ("19 / 30", 19), (26, 26), ("26", 26), ("", None), (None, None)],
)
def test_parse_cognitive_score(raw, expected):
    assert parse_cognitive_score(raw) == expected


# ---------------------------------------------------------------------------
# restrictions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ([20], [20]),
        (20, [20]),
        ("20", ["20"]),
        ("20,21", ["20", "21"]),
        ("20; 21", ["20", "21"]),
        ("", []),
        (None, []),
    ],
)
def test_normalize_restrictions(raw, expected):
    assert normalize_restrictions(raw) == expected


# ---------------------------------------------------------------------------
# Date OCR fallback
# ---------------------------------------------------------------------------

def test_normalize_dates_leaves_normal_dates_alone():
    result = normalize_dates({"dmer": {"cns.seizure_date": "24APR1932"}})
    assert result["dmer"]["cns.seizure_date"] == "1932-04-24"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("l2-3O-2O25", "2025-12-30"),
        ("O2/O5/2O25", "2025-02-05"),
        ("l4/3/l6", "2016-03-14"),
        ("l4MARl6", "2016-03-14"),
    ],
)
def test_normalize_dates_fixes_ocr_digit_confusion(raw, expected):
    result = normalize_dates({"dmer": {"x_date": raw}})
    assert result["dmer"]["x_date"] == expected


def test_normalize_dates_leaves_unparseable_value_as_is():
    result = normalize_dates({"dmer": {"x_date": "not a date at all"}})
    assert result["dmer"]["x_date"] == "not a date at all"


# ---------------------------------------------------------------------------
# alcohol_withdrawal_seizure -> epilepsy/provoked_seizure
# ---------------------------------------------------------------------------

def test_alcohol_withdrawal_seizure_forces_epilepsy_and_provoked_seizure():
    result = resolve_conflicts({
        "dmer": {
            "psychotropic_drugs.alcohol_withdrawal_seizure": True,
            "cns.epilepsy": False,
            "cns.provoked_seizure": False,
        }
    })
    assert result["dmer"]["cns.epilepsy"] is True
    assert result["dmer"]["cns.provoked_seizure"] is True


def test_alcohol_withdrawal_seizure_rule_does_not_fire_when_false():
    result = resolve_conflicts({
        "dmer": {"psychotropic_drugs.alcohol_withdrawal_seizure": False, "cns.epilepsy": False}
    })
    assert result["dmer"]["cns.epilepsy"] is False


# ---------------------------------------------------------------------------
# no_other_conditions
# ---------------------------------------------------------------------------

def test_no_other_conditions_true_when_nothing_active():
    result = check_no_other_conditions({"dmer": {"vision.cataracts": False, "details_of_condition": ""}})
    assert result["dmer"]["no_other_conditions"] is True


def test_no_other_conditions_false_when_a_checkbox_is_true():
    result = check_no_other_conditions({"dmer": {"vision.cataracts": True, "details_of_condition": ""}})
    assert result["dmer"]["no_other_conditions"] is False


def test_no_other_conditions_administrative_fields_dont_count():
    result = check_no_other_conditions({
        "dmer": {
            "restrictions": [20],
            "current_licence_class": "5",
            "blood_pressure": "120/80",
            "details_of_condition": "",
        }
    })
    assert result["dmer"]["no_other_conditions"] is True


# ---------------------------------------------------------------------------
# guide_with_no_matching_condition
# ---------------------------------------------------------------------------

def test_parse_guide_section_from_free_text_and_number():
    assert parse_guide_section("BC GUIDE 9.1 DIABETES") == 9
    assert parse_guide_section(1.5) == 1
    assert parse_guide_section(None) is None


def test_guide_matched_via_checkbox():
    result = check_guide_matching({
        "dmer": {"guide": "BC GUIDE 9.1 DIABETES", "endocrine.diabetes": True, "details_of_condition": ""}
    })
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_flagged_when_no_matching_condition_anywhere():
    result = check_guide_matching({
        "dmer": {"guide": "BC GUIDE 9.1 DIABETES", "endocrine.diabetes": False, "details_of_condition": "patient has vertigo"}
    })
    assert result["dmer"]["guide_with_no_matching_condition"] is True


def test_guide_not_flagged_when_absent():
    result = check_guide_matching({"dmer": {"details_of_condition": "patient has vertigo"}})
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_not_flagged_for_unrecognized_section():
    result = check_guide_matching({"dmer": {"guide": "BC GUIDE 16.1 UNKNOWN", "details_of_condition": ""}})
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_section_7_matches_via_any_of_its_five_categories():
    result = check_guide_matching({
        "dmer": {"guide": "BC GUIDE 7.2 VERTIGO", "vestibular.recurrent_vertigo": True, "details_of_condition": ""}
    })
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_word_boundary_regression_otherwise_does_not_match_other():
    # "other" (the generic .other field's local name) must not match inside "otherwise"
    result = check_guide_matching({
        "dmer": {
            "guide": "BC GUIDE 9.1 DIABETES",
            "endocrine.diabetes": False,
            "details_of_condition": "occasional vertigo, otherwise unremarkable",
        }
    })
    assert result["dmer"]["guide_with_no_matching_condition"] is True


# ---------------------------------------------------------------------------
# diabetes_guide_9_1
# ---------------------------------------------------------------------------


def test_parse_guide_subsection_extracts_full_token_with_letter_suffix():
    assert parse_guide_subsection("BC GUIDE 9.1 DIABETES") == "9.1"
    assert parse_guide_subsection("BC GUIDE 9.1A DIABETES") == "9.1A"
    assert parse_guide_subsection("bc guide 9.1a diabetes") == "9.1A"


def test_parse_guide_subsection_none_for_bare_section_or_absent():
    # A bare section number has no sub-item to report -- unlike
    # parse_guide_section, this must not fall back to just the section.
    assert parse_guide_subsection("BC GUIDE 9 DIABETES") is None
    assert parse_guide_subsection(9) is None
    assert parse_guide_subsection(None) is None
    assert parse_guide_subsection("") is None


def test_diabetes_guide_9_1_true_for_exact_match():
    result = check_diabetes_guide_9_1({"dmer": {"guide": "BC GUIDE 9.1 DIABETES"}})
    assert result["dmer"]["diabetes_guide_9_1"] is True


def test_diabetes_guide_9_1_true_for_9_1a_variant():
    # "9.1A" shares 9.1's outcome, so the letter suffix is ignored here.
    result = check_diabetes_guide_9_1({"dmer": {"guide": "BC GUIDE 9.1A DIABETES"}})
    assert result["dmer"]["diabetes_guide_9_1"] is True


def test_diabetes_guide_9_1_false_for_different_subsection():
    result = check_diabetes_guide_9_1({"dmer": {"guide": "BC GUIDE 9.2 DIABETES"}})
    assert result["dmer"]["diabetes_guide_9_1"] is False


def test_diabetes_guide_9_1_false_when_absent():
    result = check_diabetes_guide_9_1({"dmer": {}})
    assert result["dmer"]["diabetes_guide_9_1"] is False


# ---------------------------------------------------------------------------
# diabetes_treatment_not_indicated
# ---------------------------------------------------------------------------


def test_diabetes_treatment_not_indicated_true_when_no_checkbox_and_no_text_mention():
    result = check_diabetes_treatment_not_indicated({
        "dmer": {
            "endocrine.diabetes.diet": False,
            "endocrine.diabetes.oral_meds": False,
            "endocrine.diabetes.insulin": False,
            "endocrine.diabetes.insulin_secretagogues": False,
            "endocrine.diabetes_treatment_mentioned_in_text": False,
        }
    })
    assert result["dmer"]["diabetes_treatment_not_indicated"] is True


@pytest.mark.parametrize(
    "checkbox_field",
    [
        "endocrine.diabetes.diet",
        "endocrine.diabetes.oral_meds",
        "endocrine.diabetes.insulin",
        "endocrine.diabetes.insulin_secretagogues",
    ],
)
def test_diabetes_treatment_not_indicated_false_when_any_checkbox_true(checkbox_field):
    dmer = {
        "endocrine.diabetes.diet": False,
        "endocrine.diabetes.oral_meds": False,
        "endocrine.diabetes.insulin": False,
        "endocrine.diabetes.insulin_secretagogues": False,
        "endocrine.diabetes_treatment_mentioned_in_text": False,
        checkbox_field: True,
    }
    result = check_diabetes_treatment_not_indicated({"dmer": dmer})
    assert result["dmer"]["diabetes_treatment_not_indicated"] is False


def test_diabetes_treatment_not_indicated_false_when_mentioned_only_in_text():
    # No checkbox marked, but the LLM found treatment language in Section D --
    # must still count as "indicated", not just a bare checkbox check.
    result = check_diabetes_treatment_not_indicated({
        "dmer": {
            "endocrine.diabetes.diet": False,
            "endocrine.diabetes.oral_meds": False,
            "endocrine.diabetes.insulin": False,
            "endocrine.diabetes.insulin_secretagogues": False,
            "endocrine.diabetes_treatment_mentioned_in_text": True,
        }
    })
    assert result["dmer"]["diabetes_treatment_not_indicated"] is False


def test_diabetes_treatment_not_indicated_true_when_fields_absent():
    result = check_diabetes_treatment_not_indicated({"dmer": {}})
    assert result["dmer"]["diabetes_treatment_not_indicated"] is True


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------

def test_validate_schema_raises_on_type_mismatch():
    with pytest.raises(NormalizationValidationError):
        validate_schema({"vision.cataracts": "true"})  # string, not a real bool


def test_validate_schema_allows_restrictions_as_list():
    # Regression: restrictions is declared "str" in CONDITIONS (matching
    # what the LLM analysis prompt sees it as), but
    # apply_deterministic_field_formats always converts it to a list --
    # that's the correct final shape, not a violation. Found via a live
    # run of normalize_document, not written speculatively.
    from dmer_common.normalization.pipeline import ensure_all_fields

    result = ensure_all_fields({"dmer": {"restrictions": ["20", "21"]}})
    validate_schema(result["dmer"])  # must not raise


def test_validate_schema_passes_on_correct_types():
    # A minimal but correctly-typed dict for the fields checked; every other
    # CONDITIONS field is absent, which is itself a violation (None isn't a
    # bool) -- so this test only exercises a handful of fields directly via
    # a dict that satisfies every field's type for the check to pass overall,
    # confirming a full ensure_all_fields()-shaped output would validate.
    from dmer_common.normalization.pipeline import ensure_all_fields

    result = ensure_all_fields({"dmer": {}})
    validate_schema(result["dmer"])  # must not raise


def test_validate_evidence_present_raises_when_missing():
    with pytest.raises(NormalizationValidationError):
        validate_evidence_present({"vision.cataracts": True})


def test_validate_evidence_present_passes_when_present():
    validate_evidence_present(
        {"vision.cataracts": True, "vision.cataracts_evidence": "form says cataracts"}
    )  # must not raise


def test_validate_evidence_present_ignores_false_fields():
    validate_evidence_present({"vision.cataracts": False})  # must not raise


# Source-grounding boundary: controlled analysis output, separate verifier call.
def test_normalization_verifies_derived_conditions_and_score_against_original_source(monkeypatch):
    import json
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    fields = {"details_of_condition": "BIL CATARACT EXTRACTIONS", "cns.moca_score": "26/30"}
    updates = {
        "vision.cataracts": True,
        "vision.cataracts_evidence": "details_of_condition: BIL CATARACT EXTRACTIONS",
        "vision.cataracts_had_surgery": True,
        "vision.cataracts_had_surgery_evidence": "details_of_condition: BIL CATARACT EXTRACTIONS",
        "cns.moca_score": 26,
    }
    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": updates})
    client = Mock()
    client.complete.return_value = json.dumps({"supported": {
        "vision.cataracts": True, "vision.cataracts_had_surgery": True, "cns.moca_score": True,
    }})
    result = pipeline.normalize_document(client, fields)
    assert result["details_of_condition"] == fields["details_of_condition"]
    assert result["cns.moca_score"] == 26
    request = json.loads(client.complete.call_args.kwargs["messages"][1]["content"])
    assert request["source_fields"]["cns.moca_score"] == "26/30"
    assert request["candidates"]["cns.moca_score"]["value"] == 26


def test_nonblank_evidence_does_not_bypass_source_verification(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {
        "vision.cataracts": True, "vision.cataracts_evidence": "fabricated justification",
    }})
    client = Mock()
    client.complete.return_value = '{"supported": {"vision.cataracts": false}}'
    with pytest.raises(NormalizationValidationError, match="unsupported"):
        pipeline.normalize_document(client, {"details_of_condition": "No cataracts"})


def test_grounding_requires_complete_boolean_verdicts():
    from unittest.mock import Mock

    from dmer_common.normalization.evidence import validate_derived_values

    client = Mock()
    source = {"details_of_condition": "synthetic narrative"}
    proposed = {"vision.cataracts": True, "vision.cataracts_had_surgery": True}
    for response in ('{"supported": {"vision.cataracts": true}}',
                     '{"supported": {"vision.cataracts": "true", "vision.cataracts_had_surgery": true}}'):
        client.complete.return_value = response
        with pytest.raises(NormalizationValidationError, match="verdicts"):
            validate_derived_values(client, source, proposed, proposed)


def test_unchanged_checked_source_field_gets_direct_evidence_without_verifier(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    client = Mock()
    result = pipeline.normalize_document(client, {"vision.cataracts": "true"})
    assert result["vision.cataracts_evidence"] == "vision.cataracts: true"
    client.complete.assert_not_called()


def test_malformed_model_json_is_a_sanitized_validation_error():
    from unittest.mock import Mock

    from dmer_common.normalization.pipeline import categorize_conditions

    client = Mock()
    client.complete.return_value = 'private model response'
    with pytest.raises(NormalizationValidationError) as error:
        categorize_conditions(client, {"dmer": {}})
    assert "private model response" not in str(error.value)
