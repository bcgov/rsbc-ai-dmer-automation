"""Unit tests for dmer_common.normalization's deterministic (non-LLM) logic:
the input adapter, NYHA/MMSE-MoCA/restrictions parsers, the OCR-digit-
confusion date fallback, the alcohol-withdrawal-seizure conflict rule,
no_other_conditions, guide_with_no_matching_condition, and the output
type standardization and the evidence-presence check.

Pure functions (or functions operating on an already-built dict) -- no
Azure OpenAI call is made, so this file needs no credentials and always
runs (ported from the llm_normalization POC's
test_deterministic_normalization.py, plus new tests for the pieces added
on port: adapt_combined_fields, standardize_field_types, model-output validation
and the evidence checks).
"""

from __future__ import annotations

import pytest

from dmer_common.normalization.pipeline import (
    NormalizationValidationError,
    accept_analysis_output,
    adapt_combined_fields,
    apply_rule_engine_input_formats,
    apply_visual_acuity_thresholds,
    check_diabetes_guide_9_1,
    check_diabetes_treatment_not_indicated,
    check_guide_matching,
    check_no_other_conditions,
    derive_other_psych_diagnosis,
    missing_evidence,
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


@pytest.mark.parametrize("raw", ["not a date at all", ":_._", "__/__/____"])
def test_normalize_dates_blanks_an_unreadable_date(raw):
    # The rule engine's input schema accepts a date or "" only.
    result = normalize_dates({"dmer": {"x_date": raw}})
    assert result["dmer"]["x_date"] == ""


def test_normalize_dates_leaves_blank_dates_and_evidence_alone():
    result = normalize_dates(
        {"dmer": {"x_date": "", "x_date_evidence": 'details_of_condition: "TIA 2019"'}}
    )
    assert result["dmer"] == {
        "x_date": "",
        "x_date_evidence": 'details_of_condition: "TIA 2019"',
    }


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


# ---------------------------------------------------------------------------
# Step 3 -- schema validation of the model's output (standardize, then retry)
# ---------------------------------------------------------------------------


def test_model_output_is_standardized_before_it_is_validated():
    from dmer_common.normalization.schema import ConditionCategory

    out = accept_analysis_output(
        ConditionCategory.CARDIOVASCULAR,
        {
            "dmer": {
                "cardiovascular.lvef": "35%",
                "cardiovascular.cad": "yes",
                "cardiovascular.cad_evidence": 'details_of_condition: "CAD"',
            }
        },
    )["dmer"]
    assert out["cardiovascular.lvef"] == 35
    assert out["cardiovascular.cad"] is True
    assert out["cardiovascular.cad_evidence"] == 'details_of_condition: "CAD"'


@pytest.mark.parametrize(
    "fields",
    [
        {"cardiovascular.lvef": "severe"},  # nothing convertible in a number field
        {"cardiovascular.cad": {"nested": True}},  # wrong shape
    ],
)
def test_model_output_that_cant_be_standardized_is_invalid(fields):
    from dmer_common.normalization.errors import InvalidModelOutput
    from dmer_common.normalization.schema import ConditionCategory

    with pytest.raises(InvalidModelOutput):
        accept_analysis_output(ConditionCategory.CARDIOVASCULAR, {"dmer": fields})


def test_an_invented_field_name_is_dropped_not_retried():
    from dmer_common.normalization.schema import ConditionCategory

    out = accept_analysis_output(
        ConditionCategory.CNS,
        {"dmer": {"cns.epilepsy": True, "cns.epilepsy_has_concerns": True}},
    )
    assert out == {"dmer": {"cns.epilepsy": True}}


def test_a_concern_spelled_differently_maps_to_the_schema_field():
    from dmer_common.normalization.schema import ConditionCategory

    out = accept_analysis_output(
        ConditionCategory.TRAUMATIC_BRAIN_INJURY,
        {
            "dmer": {
                "traumatic_brain_injury_has_concerns": True,
                "traumatic_brain_injury_has_concerns_evidence": 'details_of_condition: "x"',
            }
        },
    )["dmer"]
    assert out == {
        "traumatic_brain_injury.has_concerns": True,
        "traumatic_brain_injury.has_concerns_evidence": 'details_of_condition: "x"',
    }


def test_a_real_field_from_another_category_is_dropped_not_retried():
    from dmer_common.normalization.schema import ConditionCategory

    out = accept_analysis_output(
        ConditionCategory.CARDIOVASCULAR, {"dmer": {"vision.cataracts": True}}
    )
    assert out == {"dmer": {}}


def test_invalid_model_output_is_retried_then_accepted():
    from unittest.mock import Mock

    from dmer_common.normalization.pipeline import categorize_conditions

    client = Mock()
    client.complete.side_effect = [
        "not json",
        '{"categories": "vision"}',
        '{"categories": ["vision"]}',
    ]
    categories = categorize_conditions(client, {"dmer": {}})
    assert client.complete.call_count == 3
    assert "vision" in [c.value for c in categories]


def test_model_output_still_invalid_after_retries_is_a_sanitized_validation_error():
    from unittest.mock import Mock

    from dmer_common.normalization.errors import MODEL_ATTEMPTS
    from dmer_common.normalization.pipeline import categorize_conditions

    client = Mock()
    client.complete.return_value = "private model response"
    with pytest.raises(NormalizationValidationError) as error:
        categorize_conditions(client, {"dmer": {}})
    assert client.complete.call_count == MODEL_ATTEMPTS
    assert "private model response" not in str(error.value)


# ---------------------------------------------------------------------------
# Step 4 -- evidence: per value, keep and flag
# ---------------------------------------------------------------------------


def test_missing_evidence_lists_true_fields_without_evidence():
    assert missing_evidence({"vision.cataracts": True}) == ["vision.cataracts"]
    assert (
        missing_evidence(
            {"vision.cataracts": True, "vision.cataracts_evidence": "x: y"}
        )
        == []
    )
    assert missing_evidence({"vision.cataracts": False}) == []


@pytest.mark.parametrize(
    "evidence,expected",
    [
        ('details_of_condition: "Drop attacks."', True),
        ("details_of_condition: Drop attacks", True),  # no quote marks
        ('details_of_condition: "DROP  ATTACKS"', True),  # case and spacing
        ('details_of_condition: "Drop attaks"', True),  # small OCR difference
        (
            'details_of_condition: "a diagnosed tumor, a significant condition"',
            False,
        ),  # commentary
        ('details_of_condition: "Drop attacks. *********."', False),  # not the text
        ("vestibular.drop_attacks: true", True),  # ticked checkbox
        ('psychiatric.other: "severe depression"', False),  # field is blank
    ],
)
def test_quote_check(evidence, expected):
    from dmer_common.normalization.evidence import quote_found

    source = {
        "details_of_condition": "Traumatic brain injury. Drop attacks.",
        "vestibular.drop_attacks": True,
        "psychiatric.other": "",
    }
    assert quote_found(evidence, source) is expected


def test_only_claims_are_checked_not_false_or_blank_values():
    from dmer_common.normalization.evidence import _claims

    updates = {
        "cns.dementia_has_concern": False,
        "cns.other": "",
        "cns.dementia": True,
        "cns.dementia_evidence": 'details_of_condition: "Dementia."',
    }
    claims = _claims({"details_of_condition": "Dementia."}, updates, updates)
    assert set(claims) == {"cns.dementia"}


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
    assert result["evidence_flags"] == []
    request = json.loads(client.complete.call_args.kwargs["messages"][1]["content"])
    assert request["source_fields"]["cns.moca_score"] == "26/30"
    assert request["candidates"]["cns.moca_score"]["value"] == 26


def test_unsupported_value_is_kept_and_flagged_not_rejected(monkeypatch):
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
    result = pipeline.normalize_document(
        client, {"details_of_condition": "No cataracts"}
    )
    assert result["vision.cataracts"] is True
    checks = {(f["field"], f["check"]) for f in result["evidence_flags"]}
    assert checks == {("vision.cataracts", "quote"), ("vision.cataracts", "support")}


def test_incomplete_verdicts_are_retried_then_flagged():
    from unittest.mock import Mock

    from dmer_common.normalization.errors import MODEL_ATTEMPTS
    from dmer_common.normalization.evidence import check_evidence

    client = Mock()
    source = {"details_of_condition": "cataracts, extracted"}
    proposed = {"vision.cataracts": True, "vision.cataracts_had_surgery": True}
    for response in (
        '{"supported": {"vision.cataracts": true}}',
        '{"supported": {"vision.cataracts": "true", "vision.cataracts_had_surgery": true}}',
    ):
        client.reset_mock()
        client.complete.return_value = response
        flags = check_evidence(client, source, proposed, proposed)
        assert client.complete.call_count == MODEL_ATTEMPTS
        assert {(f["field"], f["check"]) for f in flags} == {
            ("vision.cataracts", "support_unavailable"),
            ("vision.cataracts_had_surgery", "support_unavailable"),
        }


def test_unsupported_llm_concern_is_reverted_condition_is_kept(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(
        pipeline,
        "analyze_conditions",
        lambda *_: {
            "dmer": {
                "cns.intracranial_tumors": True,
                "cns.intracranial_tumors_evidence": 'details_of_condition: "Brain tumor."',
                "cns.intracranial_tumors_has_concerns": True,
                "cns.intracranial_tumors_has_concerns_evidence": 'details_of_condition: "Brain tumor."',
            }
        },
    )
    client = Mock()
    client.complete.return_value = (
        '{"supported": {"cns.intracranial_tumors": true, '
        '"cns.intracranial_tumors_has_concerns": false}}'
    )
    result = pipeline.normalize_document(
        client, {"details_of_condition": "Brain tumor."}
    )
    assert result["cns.intracranial_tumors"] is True
    assert result["cns.intracranial_tumors_has_concerns"] is False
    assert "cns.intracranial_tumors_has_concerns_evidence" not in result
    assert result["evidence_flags"] == [
        {
            "field": "cns.intracranial_tumors_has_concerns",
            "check": "support",
            "reason": "source does not support the value",
            "action": "reverted",
        }
    ]


def test_concern_the_support_check_could_not_judge_is_kept():
    from dmer_common.normalization.pipeline import revert_unsupported_concerns

    dmer = {"cns.parkinsons_has_concerns": True}
    flags = [
        {
            "field": "cns.parkinsons_has_concerns",
            "check": "support_unavailable",
            "reason": "x",
        }
    ]
    revert_unsupported_concerns(dmer, {}, {"cns.parkinsons_has_concerns": True}, flags)
    assert dmer["cns.parkinsons_has_concerns"] is True
    assert flags[0]["action"] == "kept"


def test_concern_from_the_source_form_is_never_reverted():
    from dmer_common.normalization.pipeline import revert_unsupported_concerns

    dmer = {"priority.has_concerns": True}
    flags = [{"field": "priority.has_concerns", "check": "support", "reason": "x"}]
    revert_unsupported_concerns(
        dmer, {"priority.has_concerns": True}, {"priority.has_concerns": True}, flags
    )
    assert dmer["priority.has_concerns"] is True


@pytest.mark.parametrize(
    "text,category",
    [
        ("MVP noted on echo.", "cardiovascular"),
        ("Permanent pacemaker.", "cardiovascular"),
        ("Hernia.", "general"),
        ("Spina bifida.", "musculoskeletal"),
        ("Crohns disease", "general"),
    ],
)
def test_routing_backstop_adds_a_category_named_in_the_text(text, category):
    from dmer_common.normalization.pipeline import _force_categories_from_schema_terms

    categories, seen = [], set()
    _force_categories_from_schema_terms(
        {"dmer": {"details_of_condition": text}}, categories, seen
    )
    assert category in [c.value for c in categories]


@pytest.mark.parametrize("text", ["ms. smith has a cold", "Patient is doing well."])
def test_routing_backstop_ignores_ordinary_words(text):
    from dmer_common.normalization.pipeline import _force_categories_from_schema_terms

    categories, seen = [], set()
    _force_categories_from_schema_terms(
        {"dmer": {"details_of_condition": text}}, categories, seen
    )
    assert categories == []


def test_unchanged_checked_source_field_gets_direct_evidence_without_verifier(
    monkeypatch,
):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    client = Mock()
    result = pipeline.normalize_document(client, {"vision.cataracts": "selected"})
    assert result["vision.cataracts"] is True
    assert result["vision.cataracts_evidence"] == "vision.cataracts: true"
    assert result["evidence_flags"] == []
    client.complete.assert_not_called()


# ---------------------------------------------------------------------------
# Masked model output -- words the deployment replaced with asterisks are
# restored from the input the call sent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "masked,expected",
    [
        ("Permanent *********.", "Permanent pacemaker."),
        (
            'details_of_condition: "*** noted on ****."',
            'details_of_condition: "MVP noted on echo."',
        ),
        ("******.", "Hernia."),
        ("Status post ******** extraction", "Status post cataract extraction"),
    ],
)
def test_masked_value_is_restored_from_the_call_input(masked, expected):
    from dmer_common.normalization.unmask import restore_masked

    source = {
        "details_of_condition": "Permanent pacemaker. MVP noted on echo. Hernia. Diabetes.",
        "vision.other": "status post cataract extraction",
    }
    out = restore_masked({"x": masked}, [*source.values()], step="test")
    assert out["x"].casefold() == expected.casefold()


@pytest.mark.parametrize(
    "masked",
    [
        "*********",  # several 9-letter words, no context -> ambiguous
        "Implanted *********.",  # context not in the source -> no match
    ],
)
def test_masked_value_that_cant_be_pinned_down_is_left_masked(masked):
    from dmer_common.normalization.unmask import restore_masked

    source = ["Permanent pacemaker. Psoriasis and arthritis."]
    assert restore_masked({"x": masked}, source, step="test") == {"x": masked}


def test_asterisks_that_are_really_in_the_source_are_left_alone():
    from dmer_common.normalization.unmask import restore_masked

    assert restore_masked({"x": "MoCA ***"}, ["MoCA *** (illegible)"], step="test") == {
        "x": "MoCA ***"
    }


def test_masked_field_name_is_restored_and_ties_are_broken_by_the_input():
    from dmer_common.normalization.pipeline import _ANALYSIS_KEYS
    from dmer_common.normalization.unmask import restore_masked

    out = restore_masked(
        {
            "cardiovascular.*********": True,  # only one schema name fits
            "general.******": True,  # crohns/ulcers/hernia/cancer fit
            "general.******_evidence": 'details_of_condition: "******."',
        },
        ["Hernia."],
        _ANALYSIS_KEYS,
        step="test",
    )
    assert out == {
        "cardiovascular.pacemaker": True,
        "general.hernia": True,
        "general.hernia_evidence": 'details_of_condition: "Hernia."',
    }


def test_ambiguous_masked_field_name_is_left_masked():
    from dmer_common.normalization.pipeline import _ANALYSIS_KEYS
    from dmer_common.normalization.unmask import restore_masked

    assert restore_masked(
        {"general.******": True}, ["Nothing named."], _ANALYSIS_KEYS, step="t"
    ) == {"general.******": True}


def test_analysis_call_restores_masked_output_before_validating_it():
    from unittest.mock import Mock

    from dmer_common.normalization.pipeline import analyze_condition_category
    from dmer_common.normalization.schema import ConditionCategory

    client = Mock()
    client.complete.return_value = (
        '{"dmer": {"cardiovascular.*********": true, '
        '"cardiovascular.*********_evidence": "details_of_condition: \\"Permanent *********.\\""}}'
    )
    out = analyze_condition_category(
        client,
        {"dmer": {"details_of_condition": "Permanent pacemaker."}},
        ConditionCategory.CARDIOVASCULAR,
    )["dmer"]
    assert out["cardiovascular.pacemaker"] is True
    assert (
        out["cardiovascular.pacemaker_evidence"]
        == 'details_of_condition: "Permanent pacemaker."'
    )
    assert client.complete.call_count == 1


def test_mask_logs_carry_field_names_and_outcomes_never_text(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import unmask

    log = Mock()
    monkeypatch.setattr(unmask, "_log", log)
    unmask.restore_masked(
        {
            "cardiovascular.*********_evidence": "Permanent *********.",
            "general.other": "*********",
        },
        ["Permanent pacemaker. Psoriasis."],
        {"cardiovascular.pacemaker_evidence", "general.other"},
        step="analyze:cardiovascular",
    )
    calls = log.info.call_args_list + log.warning.call_args_list
    extras = [c.kwargs["extra"] for c in calls]
    assert {(e["location"], e["outcome"]) for e in extras} == {
        ("key", "restored"),
        ("value", "restored"),
        ("value", "ambiguous"),
    }
    assert all(e["step"] == "analyze:cardiovascular" for e in extras)
    logged = repr([c.args for c in calls]) + repr(extras)
    assert "pacemaker" not in logged.replace("cardiovascular.pacemaker", "")
    assert "Psoriasis" not in logged and "Permanent" not in logged


# ---------------------------------------------------------------------------
# Text fallbacks -- conditions the model leaves out when their word is masked
# ---------------------------------------------------------------------------

_FALLBACK_REASON = "set from the source text; the model returned nothing for it"


@pytest.mark.parametrize(
    "text,field,sentence",
    [
        (
            "MVP noted on echo. TIA.",
            "cardiovascular.mitral_valve_prolapse",
            "MVP noted on echo.",
        ),
        (
            "Mitral valve prolapse.",
            "cardiovascular.mitral_valve_prolapse",
            "Mitral valve prolapse.",
        ),
        (
            "Psoriasis. Permanent pacemaker.",
            "cardiovascular.pacemaker",
            "Permanent pacemaker.",
        ),
        (
            "PPM in situ since 2019.",
            "cardiovascular.pacemaker",
            "PPM in situ since 2019.",
        ),
        ("Hernia. Type 2 diabetes.", "general.hernia", "Hernia."),
        ("COPD, on inhalers.", "respiratory.copd", "COPD, on inhalers."),
        ("C0PD.", "respiratory.copd", "C0PD."),
        ("Hx of multiple scler", "cns.multiple_sclerosis", "Hx of multiple scler"),
        ("Profound hearing loss. CMT.", "cns.charcot_marie_tooth_disease", "CMT."),
        (
            "Charcot-Marie-Tooth.",
            "cns.charcot_marie_tooth_disease",
            "Charcot-Marie-Tooth.",
        ),
        ("Crohn's disease. DDD.", "general.crohns", "Crohn's disease."),
        ("Crohns.", "general.crohns", "Crohns."),
        ("CABG in 2019.", "cardiovascular.cad", "CABG in 2019."),
        ("Coronary artery disease.", "cardiovascular.cad", "Coronary artery disease."),
        ("Bilateral cataracts.", "vision.cataracts", "Bilateral cataracts."),
        ("Nephrectomy. NIDDM.", "endocrine.diabetes", "NIDDM."),
        ("Type 2 diabetes.", "endocrine.diabetes", "Type 2 diabetes."),
        # related terms from the field's schema description
        ("Angina.", "cardiovascular.cad", "Angina."),
        ("Angioplasty last year.", "cardiovascular.cad", "Angioplasty last year."),
        ("Coronary stent 2020.", "cardiovascular.cad", "Coronary stent 2020."),
        ("Post-MI.", "cardiovascular.cad", "Post-MI."),
        ("Diabetic retinopathy.", "endocrine.diabetes", "Diabetic retinopathy."),
        ("DM on metformin.", "endocrine.diabetes", "DM on metformin."),
        (
            "Permanent pacemaker in situ.",
            "cardiovascular.pacemaker",
            "Permanent pacemaker in situ.",
        ),
        # masked and dropped when they are the only condition
        ("Colostomy.", "general.colostomy", "Colostomy."),
        ("Urostomy.", "general.uro", "Urostomy."),
        ("Spina bifida.", "musculoskeletal.spinal_bifida", "Spina bifida."),
        ("GERD.", "general.gerd", "GERD."),
        ("Acid reflux.", "general.gerd", "Acid reflux."),
        ("Leukemia.", "general.cancer", "Leukemia."),
        ("Lymphoma, in remission.", "general.cancer", "Lymphoma, in remission."),
        ("AMD.", "vision.progressive_eye_condition", "AMD."),
        (
            "Sleep apnea, CPAP compliant.",
            "sleep.cpap_compliant",
            "Sleep apnea, CPAP compliant.",
        ),
        ("Compliant with CPAP.", "sleep.cpap_compliant", "Compliant with CPAP."),
        (
            "Macular degeneration.",
            "vision.progressive_eye_condition",
            "Macular degeneration.",
        ),
    ],
)
def test_text_fallback_sets_a_condition_the_model_left_out(text, field, sentence):
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": text}
    dmer = dict(source)
    flags = apply_text_fallbacks(dmer, source, updates={})
    assert dmer[field] is True
    assert dmer[f"{field}_evidence"] == f'details_of_condition: "{sentence}"'
    assert {
        "field": field,
        "check": "text_fallback",
        "reason": _FALLBACK_REASON,
    } in flags


@pytest.mark.parametrize(
    "text",
    [
        "No hernia.",
        "Pacemaker removed 2019.",
        "Inguinal hernia repaired 2015.",
        "Family history of MVP.",
        "?MVP on echo.",
        "mvp",  # acronyms are case-sensitive
        "Herniated disc.",  # a disc herniation is not a hernia
        "MS, mild.",  # could be mitral stenosis
        "Diabetes insipidus.",  # its own field
        "Pre-diabetes.",  # its own field
        "Prediabetes.",
        "Cataracts removed 2018.",
        "Carotid stent 2020.",  # not coronary
        "Femoral angioplasty.",
        "Diabetes insipidus, on desmopressin.",
        "No COPD.",
        "Malignant hypertension.",  # not cancer
        "OSA, CPAP non-compliant.",
        "Poorly compliant with CPAP.",
    ],
)
def test_text_fallback_ignores_negated_or_unrelated_mentions(text):
    from dmer_common.normalization.pipeline import _TEXT_FALLBACKS, apply_text_fallbacks

    source = {"details_of_condition": text}
    dmer = dict(source)
    assert apply_text_fallbacks(dmer, source, updates={}) == []
    assert not any(dmer.get(field) for field in _TEXT_FALLBACKS)


@pytest.mark.parametrize(
    "text,field,value,quote",
    [
        ("Cognitive impairment, SIMARD 60.", "cns.simard_score", 60, "SIMARD 60"),
        ("SIMARD MD -5.", "cns.simard_score", -5, "SIMARD MD -5"),
        ("Cognitive impairment, MoCA 22/30.", "cns.moca_score", 22, "MoCA 22"),
        ("MMSE score 24.", "cns.mmse_score", 24, "MMSE score 24"),
    ],
)
def test_score_fallback_reads_the_number_after_the_test_name(text, field, value, quote):
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": text}
    dmer = dict(source)
    flags = apply_text_fallbacks(dmer, source, updates={})
    assert dmer[field] == value
    assert dmer[f"{field}_evidence"] == f'details_of_condition: "{quote}"'
    assert [f["field"] for f in flags] == [field]


@pytest.mark.parametrize(
    "text,dmer_extra,updates",
    [
        ("MMSE not done, 2019.", {}, {}),  # no score written
        ("MoCA 22.", {"cns.moca_score": "24/30"}, {}),  # the form's box wins
        ("MoCA 22.", {}, {"cns.moca_score": 21}),  # the model's value wins
    ],
)
def test_score_fallback_leaves_other_scores_alone(text, dmer_extra, updates):
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": text, **dmer_extra}
    dmer = {**source, **updates}
    before = dict(dmer)
    assert apply_text_fallbacks(dmer, source, updates) == []
    assert dmer == before


def test_score_fallback_fills_a_null_score_from_the_model():
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": "Cognitive impairment, SIMARD 60."}
    dmer = {**source, "cns.simard_score": None}
    apply_text_fallbacks(dmer, source, updates={"cns.simard_score": None})
    assert dmer["cns.simard_score"] == 60


def test_text_fallback_respects_what_the_model_returned():
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": "Hernia."}
    dmer = {**source, "general.hernia": False}
    assert apply_text_fallbacks(dmer, source, updates={"general.hernia": False}) == []
    assert dmer["general.hernia"] is False


def test_text_fallback_reads_other_fields_too():
    from dmer_common.normalization.pipeline import apply_text_fallbacks

    source = {"details_of_condition": "", "cardiovascular.other": "PPM"}
    dmer = dict(source)
    apply_text_fallbacks(dmer, source, updates={})
    assert dmer["cardiovascular.pacemaker_evidence"] == 'cardiovascular.other: "PPM"'


def test_text_fallback_end_to_end_is_flagged_and_skips_the_verifier(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    client = Mock()
    result = pipeline.normalize_document(
        client, {"details_of_condition": "Permanent pacemaker."}
    )
    assert result["cardiovascular.pacemaker"] is True
    assert result["cardiovascular.pacemaker_has_concerns"] is False
    assert result["no_other_conditions"] is False
    assert result["evidence_flags"] == [
        {
            "field": "cardiovascular.pacemaker",
            "check": "text_fallback",
            "reason": _FALLBACK_REASON,
            "action": "kept",
        }
    ]
    client.complete.assert_not_called()


# ---------------------------------------------------------------------------
# Normalization checklist -- deterministic behaviour each item depends on
# (the model-dependent half is in the black-box suite)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("III", 3),
        ("Class II", 2),
        ("NYHA IV", 4),
        ("lll", 3),  # handwritten I read as l
        ("NYHA class Il", 2),
        ("|V", 4),
        ("class 3", 3),
        ("V", None),  # out of range is never guessed
        ("unknown", None),
    ],
)
def test_nyha_roman_numeral_to_integer(raw, expected):
    out = standardize_field_types({"dmer": {"cardiovascular.nyha_class": raw}})
    assert out["dmer"]["cardiovascular.nyha_class"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("26/30", 26),
        ("MMSE 24", 24),
        ("24/30 on 2025-03-01", 24),
        ("28", 28),
        ("", None),
        ("not done", None),
        ("N/A", None),
        (None, None),
    ],
)
def test_mmse_is_a_single_number_or_null(raw, expected):
    out = standardize_field_types({"dmer": {"cns.mmse_score": raw}})
    assert out["dmer"]["cns.mmse_score"] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [("20, 21", [20, 21]), ("R20", [20]), ("none", []), ("", []), (None, [])],
)
def test_restrictions_become_an_integer_array_for_the_rule_engine(raw, expected):
    out = apply_rule_engine_input_formats({"dmer": {"restrictions": raw}})
    assert out["dmer"]["restrictions"] == expected


@pytest.mark.parametrize(
    "psych,drugs",
    [
        (
            "psychiatric.compliant_with_treatment",
            "psychotropic_drugs.perscribed_drugs_compliant",
        ),
        (
            "psychiatric.non_compliant_with_treatment",
            "psychotropic_drugs.perscribed_drugs_non_compliant",
        ),
    ],
)
def test_prescribed_drug_compliance_also_reads_the_psychiatric_checkbox(psych, drugs):
    out = resolve_conflicts({"dmer": {psych: True, drugs: False}})["dmer"]
    assert out[drugs] is True
    assert out[f"{drugs}_evidence"] == f"{psych}: true"


def test_prescribed_drug_compliance_is_unchanged_without_the_psychiatric_checkbox():
    dmer = {
        "psychiatric.compliant_with_treatment": False,
        "psychotropic_drugs.perscribed_drugs_compliant": True,
        "psychotropic_drugs.perscribed_drugs_compliant_evidence": "x: true",
    }
    out = resolve_conflicts({"dmer": dict(dmer)})["dmer"]
    assert out == dmer


def test_alcohol_withdrawal_seizure_is_normalized_to_a_seizure_end_to_end(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    result = pipeline.normalize_document(
        Mock(), {"psychotropic_drugs.alcohol_withdrawal_seizure": "selected"}
    )
    assert result["cns.epilepsy"] is True
    assert result["cns.provoked_seizure"] is True
    assert result["field_sources"]["cns.epilepsy"] == "rule"


def test_no_other_conditions_end_to_end(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    blank = pipeline.normalize_document(Mock(), {"details_of_condition": ""})
    assert blank["no_other_conditions"] is True
    ticked = pipeline.normalize_document(Mock(), {"vision.cataracts": "selected"})
    assert ticked["no_other_conditions"] is False


def test_multiple_conditions_from_details_are_all_kept(monkeypatch):
    import json
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    text = "Epilepsy. Type 2 diabetes on metformin. Cataracts. Sleep apnea on CPAP."
    updates = {
        "cns.epilepsy": True,
        "cns.epilepsy_evidence": 'details_of_condition: "Epilepsy."',
        "endocrine.diabetes": True,
        "endocrine.diabetes_evidence": 'details_of_condition: "Type 2 diabetes on metformin."',
        "vision.cataracts": True,
        "vision.cataracts_evidence": 'details_of_condition: "Cataracts."',
        "sleep.obstructive_sleep_apnea": True,
        "sleep.obstructive_sleep_apnea_evidence": 'details_of_condition: "Sleep apnea on CPAP."',
    }
    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": updates})
    client = Mock()
    client.complete.return_value = json.dumps(
        {"supported": {f: True for f in updates if not f.endswith("_evidence")}}
    )
    result = pipeline.normalize_document(client, {"details_of_condition": text})
    for field, value in updates.items():
        assert result[field] == value
    assert result["evidence_flags"] == []
    assert {
        result["field_sources"][f] for f in updates if not f.endswith("_evidence")
    } == {"model"}


def test_condition_written_in_details_that_is_also_a_ticked_checkbox(monkeypatch):
    import json
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    # The model sees the ticked box and the Section D mention and agrees.
    field = "sleep.obstructive_sleep_apnea"
    updates = {
        field: True,
        f"{field}_evidence": 'details_of_condition: "Sleep apnea, stable on CPAP."',
    }
    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": updates})
    client = Mock()
    client.complete.return_value = json.dumps({"supported": {field: True}})
    result = pipeline.normalize_document(
        client,
        {"details_of_condition": "Sleep apnea, stable on CPAP.", field: "selected"},
    )
    assert result[field] is True
    assert (
        result[f"{field}_evidence"]
        == 'details_of_condition: "Sleep apnea, stable on CPAP."'
    )
    assert result[f"{field}_has_concerns"] is False
    assert result["evidence_flags"] == []


def test_ticked_checkbox_stays_true_when_the_model_returns_nothing(monkeypatch):
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline

    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": {}})
    result = pipeline.normalize_document(
        Mock(), {"details_of_condition": "Cataracts.", "vision.cataracts": "selected"}
    )
    assert result["vision.cataracts"] is True
    assert result["vision.cataracts_evidence"] == "vision.cataracts: true"
    assert result["field_sources"]["vision.cataracts"] == "di"


def test_non_schema_di_checkbox_still_becomes_a_bool():
    out = adapt_combined_fields(
        {
            "hearing.hearing_aid": "selected",
            "hearing.no_hearing_aid": "unselected",
            "first_name": "x",
        }
    )["dmer"]
    assert out == {
        "hearing.hearing_aid": True,
        "hearing.no_hearing_aid": False,
        "first_name": "x",
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AGE", "age"),
        (" Age DMER ", "age"),
        ("age-related", "age"),
        ("Routine", "routine"),
        ("", ""),
        (None, ""),
    ],
)
def test_dmer_type_is_what_the_rules_compare(raw, expected):
    from dmer_common.normalization.pipeline import normalize_dmer_type

    assert normalize_dmer_type(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5", "5"),
        ("100", "1"),
        ("l", "1"),
        ("Class 5", "5"),
        ("5, 6", "5,6"),
        ("O", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_licence_class_ocr_cleanup(raw, expected):
    from dmer_common.normalization.pipeline import normalize_licence_class

    assert normalize_licence_class(raw) == expected


def test_field_sources_label_every_schema_field(monkeypatch):
    import json
    from unittest.mock import Mock

    from dmer_common.normalization import pipeline
    from dmer_common.normalization.schema import CONDITIONS

    apnea = "sleep.obstructive_sleep_apnea"
    updates = {
        apnea: True,
        f"{apnea}_evidence": 'details_of_condition: "Sleep apnea."',
        f"{apnea}_has_concerns": True,
        f"{apnea}_has_concerns_evidence": "made up",
    }
    monkeypatch.setattr(pipeline, "analyze_conditions", lambda *_: {"dmer": updates})
    client = Mock()
    client.complete.return_value = json.dumps(
        {"supported": {apnea: True, f"{apnea}_has_concerns": False}}
    )
    result = pipeline.normalize_document(
        client,
        {
            "details_of_condition": "Sleep apnea. Hernia.",
            "vision.cataracts": "selected",
            "cns.mmse_score": "26/30",
        },
    )
    sources = result["field_sources"]
    assert set(CONDITIONS) <= set(sources)
    assert sources[apnea] == "model"
    assert sources[f"{apnea}_has_concerns"] == "model_reverted"
    assert sources["general.hernia"] == "text_fallback"
    assert sources["vision.cataracts"] == "di"
    assert sources["cns.mmse_score"] == "di"
    assert sources["no_other_conditions"] == "rule"
    assert sources["cardiovascular.pacemaker"] == "default"


@pytest.mark.parametrize(
    "text",
    ["Meniere's disease. AMD. Applying for class 4.", "Diabetes. Upgrade to class 1."],
)
def test_applying_for_class_is_priority_language(text):
    from dmer_common.normalization.pipeline import _has_priority_details_signal

    assert _has_priority_details_signal({"dmer": {"details_of_condition": text}})


@pytest.mark.parametrize(
    "text,category",
    [
        ("Cognitive impairment, SIMARD 60.", "cognition"),
        ("Trails B 3 minutes 10 seconds.", "cognition"),
        ("Alzheimer's disease.", "cognition"),
        ("NIDDM.", "endocrine"),
        ("Persistent hypoglycemia unawareness.", "endocrine"),
    ],
)
def test_common_phrasings_route_to_their_category(text, category):
    from dmer_common.normalization.pipeline import (
        _force_categories_from_keywords,
        _force_categories_from_schema_terms,
    )
    from dmer_common.normalization.schema import ConditionCategory

    slim = {"dmer": {"details_of_condition": text}}
    categories, seen = [], set()
    _force_categories_from_keywords(slim, categories, seen)
    _force_categories_from_schema_terms(slim, categories, seen)
    assert ConditionCategory(category) in categories


def test_eye_nerve_palsy_concern_is_analyzed_with_vision_and_requires_evidence():
    from dmer_common.normalization.prompts import build_analysis_prompt
    from dmer_common.normalization.schema import CATEGORY_CONDITIONS, ConditionCategory

    field = "vision.eye_nerve_palsy_has_concerns"
    assert field in CATEGORY_CONDITIONS[ConditionCategory.VISION]
    assert field in build_analysis_prompt(ConditionCategory.VISION)
    assert missing_evidence({"vision.eye_nerve_palsy": False, field: True}) == [field]


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
