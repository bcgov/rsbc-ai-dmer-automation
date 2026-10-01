"""Unit tests for dmer_common.normalization's deterministic (non-LLM) logic:
the input adapter, NYHA/MMSE-MoCA/restrictions parsers, the OCR-digit-
confusion date fallback, the alcohol-withdrawal-seizure conflict rule,
no_other_conditions, guide_with_no_matching_condition, and the output
type standardization and the evidence-presence check.

Pure functions (or functions operating on an already-built dict) -- no
Azure OpenAI call is made, so this file needs no credentials and always
runs (ported from the llm_normalization POC's
test_deterministic_normalization.py, plus new tests for the pieces added
on port: adapt_combined_fields, standardize_field_types, validate_evidence_present).
"""

from __future__ import annotations

import pytest
from dmer_common.normalization.pipeline import (
    NormalizationValidationError,
    adapt_combined_fields,
    apply_rule_engine_input_formats,
    apply_visual_acuity_thresholds,
    check_diabetes_guide_9_1,
    check_diabetes_treatment_not_indicated,
    check_guide_matching,
    check_no_other_conditions,
    derive_other_psych_diagnosis,
    normalize_dates,
    normalize_restrictions,
    parse_aneurysm_size_cm,
    parse_cognitive_score,
    parse_duration_seconds,
    parse_guide_number,
    parse_guide_section,
    parse_guide_subsection,
    parse_nyha_class,
    parse_visual_acuity_denominator,
    resolve_conflicts,
    standardize_field_types,
    validate_evidence_present,
)

# ---------------------------------------------------------------------------
# adapt_combined_fields -- di-processor's real CombinedExtraction shape
# ---------------------------------------------------------------------------


def test_adapt_combined_fields_coerces_checkbox_strings_to_bool():
    adapted = adapt_combined_fields(
        {"vision.cataracts": "true", "vestibular.drop_attacks": "false"}
    )
    assert adapted["dmer"]["vision.cataracts"] is True
    assert adapted["dmer"]["vestibular.drop_attacks"] is False


def test_adapt_combined_fields_passes_through_non_bool_fields_unchanged():
    adapted = adapt_combined_fields(
        {"cardiovascular.nyha_class": "3", "vision.other": "some text"}
    )
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
    [
        ("III", 3),
        ("iv", 4),
        ("3", 3),
        (3, 3),
        ("", None),
        (None, None),
        ("V", None),
        (5, None),
        (0, None),
    ],
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
        (["20", "21"], [20, 21]),
        (20, [20]),
        (20.0, [20]),
        ("20", [20]),
        ("20,21", [20, 21]),
        ("20; 21", [20, 21]),
        ("R20, R21", [20, 21]),
        ("none", []),
        ("", []),
        (None, []),
    ],
)
def test_normalize_restrictions(raw, expected):
    # Integers, because the rule engine tests `20 in dmer.restrictions` and its
    # input schema requires integer items -- strings would silently never match.
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
    result = resolve_conflicts(
        {
            "dmer": {
                "psychotropic_drugs.alcohol_withdrawal_seizure": True,
                "cns.epilepsy": False,
                "cns.provoked_seizure": False,
            }
        }
    )
    assert result["dmer"]["cns.epilepsy"] is True
    assert result["dmer"]["cns.provoked_seizure"] is True


def test_alcohol_withdrawal_seizure_rule_does_not_fire_when_false():
    result = resolve_conflicts(
        {
            "dmer": {
                "psychotropic_drugs.alcohol_withdrawal_seizure": False,
                "cns.epilepsy": False,
            }
        }
    )
    assert result["dmer"]["cns.epilepsy"] is False


# ---------------------------------------------------------------------------
# no_other_conditions
# ---------------------------------------------------------------------------


def test_no_other_conditions_true_when_nothing_active():
    result = check_no_other_conditions(
        {"dmer": {"vision.cataracts": False, "details_of_condition": ""}}
    )
    assert result["dmer"]["no_other_conditions"] is True


def test_no_other_conditions_false_when_a_checkbox_is_true():
    result = check_no_other_conditions(
        {"dmer": {"vision.cataracts": True, "details_of_condition": ""}}
    )
    assert result["dmer"]["no_other_conditions"] is False


def test_no_other_conditions_administrative_fields_dont_count():
    result = check_no_other_conditions(
        {
            "dmer": {
                "restrictions": [20],
                "current_licence_class": "5",
                "blood_pressure": "120/80",
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["no_other_conditions"] is True


def test_no_other_conditions_ignores_opinion_priority_and_recommendations():
    result = check_no_other_conditions(
        {
            "dmer": {
                "opinion.yes": True,
                "priority.should_not_drive": True,
                "recommendations.road_test_to_assess": True,
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["no_other_conditions"] is True


def test_non_vision_condition_indicated_true_for_a_non_vision_condition():
    result = check_no_other_conditions(
        {"dmer": {"psychiatric.bipolar": True, "details_of_condition": ""}}
    )
    assert result["dmer"]["non_vision_condition_indicated"] is True


def test_non_vision_condition_indicated_false_for_vision_only_or_section_d_only():
    vision_only = check_no_other_conditions(
        {"dmer": {"vision.color_blindness": True, "details_of_condition": ""}}
    )
    assert vision_only["dmer"]["non_vision_condition_indicated"] is False
    assert vision_only["dmer"]["no_other_conditions"] is False
    text_only = check_no_other_conditions(
        {"dmer": {"details_of_condition": "cataracts both eyes"}}
    )
    assert text_only["dmer"]["non_vision_condition_indicated"] is False


def test_non_vision_condition_indicated_ignores_opinion_and_priority():
    result = check_no_other_conditions(
        {
            "dmer": {
                "opinion.yes": True,
                "priority.has_concerns": True,
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["non_vision_condition_indicated"] is False


def test_no_other_conditions_ignores_acuity_and_field_results():
    result = check_no_other_conditions(
        {
            "dmer": {
                "visual_acuity.corrected_both": "20/200",
                "visual_acuity.corrected_vision_20/80_or_worse": True,
                "visual_field.abnormal": True,
                "visual_field.meet_criteria_for_licence_class_yes": True,
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["no_other_conditions"] is True


def test_cataracts_or_retinopathy_mark_progressive_eye_condition():
    from dmer_common.normalization.pipeline import flag_progressive_eye_condition

    for source in ("vision.cataracts", "vision.retinopathy"):
        dmer = flag_progressive_eye_condition({"dmer": {source: True}})["dmer"]
        assert dmer["vision.progressive_eye_condition"] is True
        assert dmer["vision.progressive_eye_condition_evidence"] == f"{source}: true"
    assert (
        "vision.progressive_eye_condition"
        not in flag_progressive_eye_condition({"dmer": {}})["dmer"]
    )


# ---------------------------------------------------------------------------
# guide_with_no_matching_condition
# ---------------------------------------------------------------------------


def test_parse_guide_section_from_free_text_and_number():
    assert parse_guide_section("BC GUIDE 9.1 DIABETES") == 9
    assert parse_guide_section(1.5) == 1
    assert parse_guide_section(None) is None


def test_guide_matched_via_checkbox():
    result = check_guide_matching(
        {
            "dmer": {
                "guide": "BC GUIDE 9.1 DIABETES",
                "endocrine.diabetes": True,
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_flagged_when_no_matching_condition_anywhere():
    result = check_guide_matching(
        {
            "dmer": {
                "guide": "BC GUIDE 9.1 DIABETES",
                "endocrine.diabetes": False,
                "details_of_condition": "patient has vertigo",
            }
        }
    )
    assert result["dmer"]["guide_with_no_matching_condition"] is True


def test_guide_not_flagged_when_absent():
    result = check_guide_matching(
        {"dmer": {"details_of_condition": "patient has vertigo"}}
    )
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_not_flagged_for_unrecognized_section():
    result = check_guide_matching(
        {"dmer": {"guide": "BC GUIDE 16.1 UNKNOWN", "details_of_condition": ""}}
    )
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_section_7_matches_via_any_of_its_five_categories():
    result = check_guide_matching(
        {
            "dmer": {
                "guide": "BC GUIDE 7.2 VERTIGO",
                "vestibular.recurrent_vertigo": True,
                "details_of_condition": "",
            }
        }
    )
    assert result["dmer"]["guide_with_no_matching_condition"] is False


def test_guide_word_boundary_regression_otherwise_does_not_match_other():
    # "other" (the generic .other field's local name) must not match inside "otherwise"
    result = check_guide_matching(
        {
            "dmer": {
                "guide": "BC GUIDE 9.1 DIABETES",
                "endocrine.diabetes": False,
                "details_of_condition": "occasional vertigo, otherwise unremarkable",
            }
        }
    )
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
# rule-engine input formats (guide number, integer restrictions)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("BC GUIDE 17 GENERAL DEBILITY", 17),
        ("BC GUIDE 9.1 DIABETES", 9.1),
        ("BC GUIDE 9.1A DIABETES", 9.1),
        ("1.5", 1.5),
        (1.5, 1.5),
        (17, 17),
        ("", None),
        (None, None),
        ("no guide", None),
    ],
)
def test_parse_guide_number(raw, expected):
    assert parse_guide_number(raw) == expected


def test_rule_engine_formats_convert_guide_to_a_number():
    dmer = apply_rule_engine_input_formats(
        {"dmer": {"guide": "BC GUIDE 17 GENERAL DEBILITY"}}
    )["dmer"]
    assert dmer["guide"] == 17 and isinstance(dmer["guide"], int)


def test_rule_engine_formats_drop_guide_when_absent():
    # The rule engine's input schema allows neither "" nor null for guide.
    for raw in ("", None, "no number here"):
        dmer = apply_rule_engine_input_formats({"dmer": {"guide": raw}})["dmer"]
        assert "guide" not in dmer


def test_rule_engine_formats_make_restrictions_integer_list():
    assert apply_rule_engine_input_formats({"dmer": {"restrictions": "20, 21"}})[
        "dmer"
    ]["restrictions"] == [20, 21]
    # never set, or left as ensure_all_fields' str default
    assert apply_rule_engine_input_formats({"dmer": {}})["dmer"]["restrictions"] == []
    assert (
        apply_rule_engine_input_formats({"dmer": {"restrictions": ""}})["dmer"][
            "restrictions"
        ]
        == []
    )


def test_diabetes_guide_9_1_still_sees_letter_suffix_before_guide_becomes_a_number():
    result = check_diabetes_guide_9_1({"dmer": {"guide": "BC GUIDE 9.1A DIABETES"}})
    result = apply_rule_engine_input_formats(result)
    assert result["dmer"]["diabetes_guide_9_1"] is True
    assert result["dmer"]["guide"] == 9.1


# ---------------------------------------------------------------------------
# diabetes_treatment_not_indicated
# ---------------------------------------------------------------------------


def test_diabetes_treatment_not_indicated_true_when_no_checkbox_and_no_text_mention():
    result = check_diabetes_treatment_not_indicated(
        {
            "dmer": {
                "endocrine.diabetes.diet": False,
                "endocrine.diabetes.oral_meds": False,
                "endocrine.diabetes.insulin": False,
                "endocrine.diabetes.insulin_secretagogues": False,
                "endocrine.diabetes_treatment_mentioned_in_text": False,
            }
        }
    )
    assert result["dmer"]["endocrine.diabetes_treatment_not_indicated"] is True


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
    assert result["dmer"]["endocrine.diabetes_treatment_not_indicated"] is False


def test_diabetes_treatment_not_indicated_false_when_mentioned_only_in_text():
    # No checkbox marked, but the LLM found treatment language in Section D --
    # must still count as "indicated", not just a bare checkbox check.
    result = check_diabetes_treatment_not_indicated(
        {
            "dmer": {
                "endocrine.diabetes.diet": False,
                "endocrine.diabetes.oral_meds": False,
                "endocrine.diabetes.insulin": False,
                "endocrine.diabetes.insulin_secretagogues": False,
                "endocrine.diabetes_treatment_mentioned_in_text": True,
            }
        }
    )
    assert result["dmer"]["endocrine.diabetes_treatment_not_indicated"] is False


def test_diabetes_treatment_not_indicated_true_when_fields_absent():
    result = check_diabetes_treatment_not_indicated({"dmer": {}})
    assert result["dmer"]["endocrine.diabetes_treatment_not_indicated"] is True


# ---------------------------------------------------------------------------
# parse_visual_acuity_denominator
# ---------------------------------------------------------------------------


def test_parse_visual_acuity_denominator_plain_fraction():
    assert parse_visual_acuity_denominator("20/50") == 50


def test_parse_visual_acuity_denominator_tolerates_surrounding_spaces():
    assert parse_visual_acuity_denominator("20 / 50") == 50


@pytest.mark.parametrize(
    "raw",
    ["20/50cf", "20/50CF", "20/50 cf", "20/50^cf^", "20/50 CF (counting fingers)"],
)
def test_parse_visual_acuity_denominator_strips_letter_superscripts(raw):
    # "cf" (counting fingers) and similar letter-coded annotations must not
    # affect the extracted denominator -- the digit run stops at the first
    # non-digit character.
    assert parse_visual_acuity_denominator(raw) == 50


def test_parse_visual_acuity_denominator_embeds_in_longer_text():
    assert parse_visual_acuity_denominator("Best corrected acuity 20/40 OD") == 40


@pytest.mark.parametrize("raw", [None, "", "no fraction here", "20-50", False, True])
def test_parse_visual_acuity_denominator_none_when_absent_or_malformed(raw):
    assert parse_visual_acuity_denominator(raw) is None


def test_parse_visual_acuity_denominator_numeric_suffix_is_a_known_limitation():
    # Documented limitation, not a bug: a bare numeric suffix with no
    # separator is ambiguous and is read as part of the denominator, not a
    # superscript -- every real BC Guide example uses a letter code instead.
    assert parse_visual_acuity_denominator("20/502") == 502


# ---------------------------------------------------------------------------
# apply_visual_acuity_thresholds
# ---------------------------------------------------------------------------


def test_corrected_only_field_ignores_uncorrected_when_no_corrected_value():
    # corrected_vision_20/80_or_worse must never fall back to uncorrected --
    # its name says "corrected" for a reason.
    result = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.uncorrected_both": "20/200"}}
    )
    assert result["dmer"]["visual_acuity.corrected_vision_20/80_or_worse"] is False
    assert (
        "visual_acuity.corrected_vision_20/80_or_worse_evidence" not in result["dmer"]
    )


def test_corrected_only_field_true_from_corrected_both():
    result = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.corrected_both": "20/80"}}
    )
    assert result["dmer"]["visual_acuity.corrected_vision_20/80_or_worse"] is True
    assert result["dmer"]["visual_acuity.corrected_vision_20/80_or_worse_evidence"] == (
        "visual_acuity.corrected_both=20/80"
    )


def test_both_eyes_value_wins_over_left_and_right_when_present():
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_both": "20/20",
                "visual_acuity.corrected_left": "20/200",
                "visual_acuity.corrected_right": "20/200",
            }
        }
    )
    # "both" says great vision; if left/right (bad) were consulted instead,
    # this would wrongly come out True.
    assert result["dmer"]["visual_acuity.vision_20/60_or_worse"] is False


def test_falls_back_to_better_eye_of_left_and_right_when_both_is_absent():
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_left": "20/100",
                "visual_acuity.corrected_right": "20/40",
            }
        }
    )
    # "or_better" thresholds should use the BETTER eye (20/40), not the worse.
    assert result["dmer"]["visual_acuity.vision_20/50_or_better"] is True


def test_bad_eye_field_falls_back_to_worse_eye_not_better_eye():
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_left": "20/50",
                "visual_acuity.corrected_right": "20/150",
            }
        }
    )
    # bad_eye_20/100_or_worse must pick the WORSE eye (20/150), unlike
    # every other threshold field's better-eye fallback.
    assert result["dmer"]["visual_acuity.bad_eye_20/100_or_worse"] is True


def test_bad_eye_field_false_when_worse_eye_still_meets_threshold():
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_left": "20/50",
                "visual_acuity.corrected_right": "20/90",
            }
        }
    )
    assert result["dmer"]["visual_acuity.bad_eye_20/100_or_worse"] is False


def test_non_corrected_field_falls_back_to_uncorrected_when_no_corrected_at_all():
    result = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.uncorrected_both": "20/70"}}
    )
    assert result["dmer"]["visual_acuity.vision_20/60_or_worse"] is True


def test_non_corrected_field_does_not_mix_corrected_and_uncorrected():
    # A corrected_both value exists (even though it doesn't meet the
    # threshold on its own) -- uncorrected must not be consulted at all.
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_both": "20/30",
                "visual_acuity.uncorrected_both": "20/400",
            }
        }
    )
    assert result["dmer"]["visual_acuity.vision_20/60_or_worse"] is False


@pytest.mark.parametrize(
    ("field", "boundary_raw", "expected"),
    [
        ("visual_acuity.vision_20/60_or_worse", "20/60", True),
        ("visual_acuity.vision_20/60_or_worse", "20/50", False),
        ("visual_acuity.vision_20/30_or_better", "20/30", True),
        ("visual_acuity.vision_20/30_or_better", "20/40", False),
        ("visual_acuity.vision_20/20_or_better", "20/20", True),
        ("visual_acuity.vision_20/20_or_better", "20/30", False),
        ("visual_acuity.bad_eye_20/100_or_worse", "20/100", True),
        ("visual_acuity.bad_eye_20/100_or_worse", "20/99", False),
    ],
)
def test_threshold_boundaries_are_exact(field, boundary_raw, expected):
    # The bad-eye field reads individual eyes only, never the both-eyes value.
    source = "corrected_right" if "bad_eye" in field else "corrected_both"
    result = apply_visual_acuity_thresholds(
        {"dmer": {f"visual_acuity.{source}": boundary_raw}}
    )
    assert result["dmer"][field] is expected


def test_bad_eye_field_ignores_both_eyes_value_that_hides_the_bad_eye():
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_both": "20/20",
                "visual_acuity.corrected_left": "20/20",
                "visual_acuity.corrected_right": "20/400",
            }
        }
    )
    dmer = result["dmer"]
    assert dmer["visual_acuity.bad_eye_20/100_or_worse"] is True
    assert (
        dmer["visual_acuity.bad_eye_20/100_or_worse_evidence"]
        == "visual_acuity.corrected_right=20/400"
    )
    # Other thresholds still use the both-eyes value.
    assert dmer["visual_acuity.vision_20/60_or_worse"] is False


def test_all_threshold_fields_false_and_evidence_free_when_no_acuity_data_at_all():
    result = apply_visual_acuity_thresholds({"dmer": {}})
    dmer = result["dmer"]
    for field in (
        "visual_acuity.corrected_vision_20/80_or_worse",
        "visual_acuity.corrected_vision_20/60_or_worse",
        "visual_acuity.corrected_vision_20/50_or_better",
        "visual_acuity.corrected_vision_20/30_or_better",
        "visual_acuity.corrected_vision_20/20_or_better",
        "visual_acuity.vision_20/60_or_worse",
        "visual_acuity.vision_20/40_or_worse",
        "visual_acuity.vision_20/50_or_better",
        "visual_acuity.vision_20/30_or_better",
        "visual_acuity.vision_20/20_or_better",
        "visual_acuity.bad_eye_20/100_or_worse",
    ):
        assert dmer[field] is False
        assert f"{field}_evidence" not in dmer


@pytest.mark.parametrize(
    ("corrected_raw", "expected"),
    [
        # (20/50_or_better, 20/30_or_better, 20/20_or_better)
        ("20/15", (True, True, True)),
        ("20/20", (True, True, True)),
        ("20/25", (True, True, False)),
        ("20/30", (True, True, False)),
        ("20/40", (True, False, False)),
        ("20/50", (True, False, False)),
        ("20/60", (False, False, False)),
    ],
)
def test_corrected_or_better_fields_band_the_corrected_value(corrected_raw, expected):
    dmer = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.corrected_both": corrected_raw}}
    )["dmer"]
    got = tuple(
        dmer[f"visual_acuity.corrected_vision_20/{n}_or_better"] for n in (50, 30, 20)
    )
    assert got == expected


def test_corrected_or_better_fields_never_use_uncorrected():
    # Progressive-eye rules rely on these meaning "a CORRECTED value is in
    # this band" -- an uncorrected-only DMER must leave all three false.
    dmer = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.uncorrected_both": "20/20"}}
    )["dmer"]
    for n in (50, 30, 20):
        assert dmer[f"visual_acuity.corrected_vision_20/{n}_or_better"] is False
    assert (
        dmer["visual_acuity.vision_20/20_or_better"] is True
    )  # the generic field still falls back


def test_corrected_or_better_fields_use_better_eye_when_both_absent():
    dmer = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_left": "20/100",
                "visual_acuity.corrected_right": "20/25",
            }
        }
    )["dmer"]
    assert dmer["visual_acuity.corrected_vision_20/30_or_better"] is True
    assert (
        dmer["visual_acuity.corrected_vision_20/30_or_better_evidence"]
        == "visual_acuity.corrected_right=20/25"
    )


def test_superscript_annotation_does_not_break_threshold_comparison():
    # "20/50cf" must compare as 50, not fail to parse and silently drop
    # out of the True case.
    result = apply_visual_acuity_thresholds(
        {"dmer": {"visual_acuity.corrected_both": "20/50cf"}}
    )
    assert result["dmer"]["visual_acuity.vision_20/50_or_better"] is True


def test_overwrites_whatever_the_llm_itself_had_set():
    # The whole point: a wrong LLM guess must not survive this pass.
    result = apply_visual_acuity_thresholds(
        {
            "dmer": {
                "visual_acuity.corrected_both": "20/200",
                "visual_acuity.vision_20/30_or_better": True,  # LLM's (wrong) guess
            }
        }
    )
    assert result["dmer"]["visual_acuity.vision_20/30_or_better"] is False


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def test_standardize_converts_every_field_to_its_declared_type():
    from dmer_common.normalization.pipeline import ensure_all_fields

    dmer = ensure_all_fields({"dmer": {}})["dmer"]
    dmer.update(
        {
            "vision.cataracts": "true",  # bool from text
            "vestibular.drop_attacks": "no",
            "cardiovascular.lvef": "35%",  # int from text
            "endocrine.HbA1C": "12.5 %",  # float from text
            "sleep.epworth_score": "",  # blank -> None
            "opinion.maybe_followup_years": "2 years",
            "hearing.hearing_db_left": "N/A",
            "cardiovascular.other": None,  # text never None
        }
    )
    out = standardize_field_types({"dmer": dmer})["dmer"]
    assert out["vision.cataracts"] is True
    assert out["vestibular.drop_attacks"] is False
    assert out["cardiovascular.lvef"] == 35
    assert out["endocrine.HbA1C"] == 12.5
    assert out["sleep.epworth_score"] is None
    assert out["opinion.maybe_followup_years"] == 2
    assert out["hearing.hearing_db_left"] is None
    assert out["cardiovascular.other"] == ""


def test_standardize_leaves_an_unreadable_number_blank_instead_of_failing():
    out = standardize_field_types({"dmer": {"cardiovascular.lvef": "illegible"}})[
        "dmer"
    ]
    assert out["cardiovascular.lvef"] is None


def test_standardize_keeps_restrictions_as_codes():
    out = standardize_field_types({"dmer": {"restrictions": [20, 21]}})["dmer"]
    assert out["restrictions"] == [20, 21]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("190", 190),
        ("190 s", 190),
        ("3:10", 190),
        ("3 min 10 sec", 190),
        ("3 minutes 10 seconds", 190),
        ("3 minutes", 180),
        (95, 95),
        ("", None),
        ("failed", None),
    ],
)
def test_parse_duration_seconds(raw, expected):
    assert parse_duration_seconds(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("5.0 cm", 5.0), ("55 mm", 5.5), ("5,5", 5.5), (6, 6.0), ("", None)],
)
def test_parse_aneurysm_size_cm(raw, expected):
    assert parse_aneurysm_size_cm(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("NYHA III", 3), ("class 2", 2), ("5", None), ("IV", 4)]
)
def test_nyha_in_text_is_read_but_never_out_of_range(raw, expected):
    out = standardize_field_types({"dmer": {"cardiovascular.nyha_class": raw}})["dmer"]
    assert out["cardiovascular.nyha_class"] == expected


def test_adapt_renames_the_di_licence_class_field():
    dmer = adapt_combined_fields({"current_license_class": "1"})["dmer"]
    assert dmer["current_licence_class"] == "1"
    assert "current_license_class" not in dmer


def test_adapt_blank_alias_does_not_overwrite_a_real_value():
    dmer = adapt_combined_fields(
        {"current_licence_class": "1", "current_license_class": ""}
    )["dmer"]
    assert dmer["current_licence_class"] == "1"


def test_adapt_turns_blank_numbers_into_none_and_keeps_written_ones():
    dmer = adapt_combined_fields(
        {"cardiovascular.lvef": "", "sleep.ahi_score": "approx 35"}
    )["dmer"]
    assert dmer["cardiovascular.lvef"] is None
    assert dmer["sleep.ahi_score"] == "approx 35"


@pytest.mark.parametrize("raw", ["true", "TRUE", "yes", "x", "selected"])
def test_adapt_accepts_any_truthy_checkbox_spelling(raw):
    assert (
        adapt_combined_fields({"vision.cataracts": raw})["dmer"]["vision.cataracts"]
        is True
    )


def test_other_psych_diagnosis_checkbox_marks_it():
    dmer = adapt_combined_fields({"psychiatric.other_psych_diagnosis": "true"})
    assert (
        derive_other_psych_diagnosis(dmer)["dmer"]["psychiatric.other_psych_diagnosis"]
        is True
    )


@pytest.mark.parametrize("field", ["psychiatric.other", "psychiatric.psych_diagnosis"])
def test_other_psych_diagnosis_from_an_unrecognised_written_diagnosis(field):
    dmer = adapt_combined_fields(
        {"psychiatric.other_psych_diagnosis": "false", field: "severe depression"}
    )
    out = derive_other_psych_diagnosis(dmer)["dmer"]
    assert out["psychiatric.other_psych_diagnosis"] is True
    assert (
        out["psychiatric.other_psych_diagnosis_evidence"]
        == f"{field}: severe depression"
    )


def test_other_psych_diagnosis_not_set_when_the_written_diagnosis_is_a_named_one():
    dmer = adapt_combined_fields(
        {"psychiatric.psych_diagnosis": "bipolar", "psychiatric.bipolar": "true"}
    )
    assert (
        derive_other_psych_diagnosis(dmer)["dmer"]["psychiatric.other_psych_diagnosis"]
        is False
    )


def test_other_psych_diagnosis_false_when_nothing_is_written():
    dmer = adapt_combined_fields(
        {"psychiatric.other_psych_diagnosis": "false", "psychiatric.other": "N/A"}
    )
    assert (
        derive_other_psych_diagnosis(dmer)["dmer"]["psychiatric.other_psych_diagnosis"]
        is False
    )


def test_normalize_document_accepts_raw_di_strings(monkeypatch):
    """DI sends every value as text, blanks included -- none of it may fail the document."""
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    fields = {
        "current_license_class": "1",
        "cardiovascular.lvef": "",
        "cns.trails_b_seconds": "3 min 10 sec",
        "cns.moca_score": "26/30",
        "endocrine.HbA1C": "",
        "opinion.maybe_followup_years": "",
        "psychiatric.other_psych_diagnosis": "false",
        "vision.cataracts": "false",
        "details_of_condition": "",
    }
    result = pipeline.normalize_document(Mock(), fields)
    assert result["current_licence_class"] == "1"
    assert result["cardiovascular.lvef"] is None
    assert result["cns.trails_b_seconds"] == 190
    assert result["cns.moca_score"] == 26
    assert result["psychiatric.other_psych_diagnosis"] is False


def test_blank_di_document_has_no_conditions(monkeypatch):
    """Every DI checkbox "false" and every DI text field "" -- including the
    other-psych-diagnosis checkbox, which the schema used to read as text --
    must come out as an empty DMER, not a condition."""
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline
    from dmer_common.normalization.schema import CONDITIONS

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    fields = {name: "" for name in CONDITIONS}
    fields.update(
        {name: "false" for name, cfg in CONDITIONS.items() if cfg["type"] == "bool"}
    )
    fields["current_license_class"] = "5"
    result = pipeline.normalize_document(Mock(), fields)
    assert result["psychiatric.other_psych_diagnosis"] is False
    assert result["no_other_conditions"] is True
    assert result["non_vision_condition_indicated"] is False


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
def test_normalization_verifies_derived_conditions_and_score_against_original_source(
    monkeypatch,
):
    import json
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    fields = {
        "details_of_condition": "BIL CATARACT EXTRACTIONS",
        "cns.moca_score": "26/30",
    }
    updates = {
        "vision.cataracts": True,
        "vision.cataracts_evidence": "details_of_condition: BIL CATARACT EXTRACTIONS",
        "vision.cataracts_had_surgery": True,
        "vision.cataracts_had_surgery_evidence": "details_of_condition: BIL CATARACT EXTRACTIONS",
        "cns.moca_score": 26,
    }
    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": updates})
    client = Mock()
    client.complete.return_value = json.dumps(
        {
            "supported": {
                "vision.cataracts": True,
                "vision.cataracts_had_surgery": True,
                "cns.moca_score": True,
            }
        }
    )
    result = pipeline.normalize_document(client, fields)
    assert result["details_of_condition"] == fields["details_of_condition"]
    assert result["cns.moca_score"] == 26
    request = json.loads(client.complete.call_args.kwargs["messages"][1]["content"])
    assert request["source_fields"]["cns.moca_score"] == "26/30"
    assert request["candidates"]["cns.moca_score"]["value"] == 26


def test_nonblank_evidence_does_not_bypass_source_verification(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(
        pipeline,
        "analyze_conditions",
        lambda *_: {
            "dmer": {
                "vision.cataracts": True,
                "vision.cataracts_evidence": "fabricated justification",
            }
        },
    )
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
    for response in (
        '{"supported": {"vision.cataracts": true}}',
        '{"supported": {"vision.cataracts": "true", "vision.cataracts_had_surgery": true}}',
    ):
        client.complete.return_value = response
        with pytest.raises(NormalizationValidationError, match="verdicts"):
            validate_derived_values(client, source, proposed, proposed)


def test_unchanged_checked_source_field_gets_direct_evidence_without_verifier(
    monkeypatch,
):
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
    client.complete.return_value = "private model response"
    with pytest.raises(NormalizationValidationError) as error:
        categorize_conditions(client, {"dmer": {}})
    assert "private model response" not in str(error.value)


def test_eye_nerve_palsy_concern_is_analyzed_with_vision_and_requires_evidence():
    from dmer_common.normalization.prompts import build_analysis_prompt
    from dmer_common.normalization.schema import CATEGORY_CONDITIONS, ConditionCategory

    field = "vision.eye_nerve_palsy_has_concerns"
    assert field in CATEGORY_CONDITIONS[ConditionCategory.VISION]
    assert field in build_analysis_prompt(ConditionCategory.VISION)
    with pytest.raises(NormalizationValidationError):
        validate_evidence_present({"vision.eye_nerve_palsy": False, field: True})


# ---------------------------------------------------------------------------
# Partial dates resolve to the most recent possible day (higher risk)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2025", "2025-12-31"),  # could have been Dec 2025
        ("March 2019", "2019-03-31"),
        ("June", "2026-06-30"),  # most recent June
        ("October", "2025-10-31"),  # October 2026 hasn't happened yet
        ("2026", "2026-09-25"),  # never past today
        ("September 2026", "2026-09-25"),
        ("2026-09-30", "2026-09-30"),  # a fully written date is kept as-is
    ],
)
def test_partial_dates_resolve_to_latest_possible_day(raw, expected):
    from datetime import date

    result = normalize_dates(
        {"dmer": {"vision.monocular_date": raw}}, today=date(2026, 9, 25)
    )
    assert result["dmer"]["vision.monocular_date"] == expected


def test_bad_eye_20_100_or_worse_marks_monocular_with_evidence():
    from dmer_common.normalization.pipeline import flag_monocular_from_bad_eye

    result = flag_monocular_from_bad_eye(
        {
            "dmer": {
                "visual_acuity.corrected_left": "20/20",
                "visual_acuity.corrected_right": "20/100",
            }
        }
    )
    assert result["dmer"]["vision.monocular"] is True
    assert (
        "visual_acuity.corrected_right=20/100"
        in result["dmer"]["vision.monocular_evidence"]
    )
    unaffected = flag_monocular_from_bad_eye(
        {"dmer": {"visual_acuity.corrected_right": "20/80"}}
    )
    assert "vision.monocular" not in unaffected["dmer"]


def test_monocular_from_acuity_reaches_the_concerns_analysis(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    seen = {}

    def analyze(_openai, dmer_input):
        seen.update(dmer_input["dmer"])
        return {"dmer": {}}

    monkeypatch.setattr(pipeline, "analyze_conditions", analyze)
    result = pipeline.normalize_document(
        Mock(),
        {
            "visual_acuity.corrected_left": "20/20",
            "visual_acuity.corrected_right": "20/200",
        },
    )
    assert seen["vision.monocular"] is True
    assert result["vision.monocular"] is True
    assert result["visual_acuity.bad_eye_20/100_or_worse"] is True
