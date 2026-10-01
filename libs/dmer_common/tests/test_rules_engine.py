"""Rule Engine: rules.json evaluated through dmer_common.rules, the way the activity runs it.

Inputs are built from the real normalization schema defaults and its deterministic
passes, so derived fields (acuity thresholds, monocular-from-bad-eye, progressive
eye condition, no_other_conditions, diabetes flags, restriction/guide formats) look
exactly like what Normalize produces. Each row case names the decision-table row it
expects to fire (or not), matched by that row's reason text.
"""

from __future__ import annotations

import itertools
import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import zen
from dateutil.relativedelta import relativedelta
from dmer_common.normalization.pipeline import (
    apply_rule_engine_input_formats,
    apply_visual_acuity_thresholds,
    check_diabetes_guide_9_1,
    check_diabetes_treatment_not_indicated,
    check_no_other_conditions,
    derive_other_psych_diagnosis,
    flag_monocular_from_bad_eye,
    flag_progressive_eye_condition,
)
from dmer_common.normalization.schema import (
    CONDITIONS,
    META_FIELD_DEFAULTS,
    TYPE_DEFAULTS,
)
from dmer_common.rules import (
    OUTCOME_PRIORITY,
    Outcome,
    RuleEvaluationError,
    Ruleset,
    RulesetError,
    select_outcome,
)

RULES_PATH = (
    Path(__file__).resolve().parents[3]
    / "services"
    / "rule-engine"
    / "rules"
    / "rules.json"
)
RULES_JSON = RULES_PATH.read_text(encoding="utf-8")
RULESET = Ruleset(RULES_JSON)
_TABLES = {
    n["name"]: n["content"]
    for n in json.loads(RULES_JSON)["nodes"]
    if n["type"] == "decisionTableNode"
}

TODAY = datetime.now(UTC).date()
N, H = "5", "1"  # licence classes -> NORMAL / HIGH (see DetermineLicenseCategory)
VF_NORMAL = {"visual_field.normal": True}


def ago(**kw) -> str:
    return (TODAY - relativedelta(**kw)).isoformat()


def base_dmer(licence: str = N) -> dict:
    """Every normalization field at its default, plus the DI pass-through fields the rules read."""
    dmer = {field: TYPE_DEFAULTS[cfg["type"]] for field, cfg in CONDITIONS.items()}
    dmer.update(META_FIELD_DEFAULTS)
    dmer.update(
        {
            "current_licence_class": licence,
            "restrictions": [],
            "details_of_condition": "",
            "medical_examination_date": ago(days=10),
            "guide": "",
            "type": "age",
        }
    )
    return dmer


def normalize(dmer: dict) -> dict:
    result = {"dmer": dmer}
    for step in (
        apply_visual_acuity_thresholds,
        flag_monocular_from_bad_eye,
        flag_progressive_eye_condition,
        derive_other_psych_diagnosis,
        check_no_other_conditions,
        check_diabetes_guide_9_1,
        check_diabetes_treatment_not_indicated,
        apply_rule_engine_input_formats,
    ):
        result = step(result)
    return result["dmer"]


def evaluate(licence: str, fields: dict):
    fields = dict(fields)
    received = fields.pop("received_date", None)
    dmer = base_dmer(licence)
    dmer.update(fields)
    return RULESET.evaluate(
        normalize(dmer), received_date=(received or ago(days=5)) + "T07:11:00Z"
    )


def row_fired(evaluation, table: str, row: int) -> bool:
    content = _TABLES[table]
    reason_id = next(o["id"] for o in content["outputs"] if o["field"] == "reason")
    reason = json.loads(content["rules"][row - 1][reason_id].strip(), strict=False)
    return any(
        o.section == content["outputPath"] and o.reason == reason
        for o in evaluation.outcomes
    )


def vision_action(evaluation) -> str:
    actions = [o.action for o in evaluation.outcomes if o.section == "vision"]
    return max(actions, key=OUTCOME_PRIORITY.index) if actions else "-"


# ---------------------------------------------------------------- vision (final vision action)

SCENARIOS = [
    # (name, licence, fields, expected final vision action)
    ("VF abnormal only, nothing else", N, {"visual_field.abnormal": True}, "IN"),
    (
        "VF abnormal + concerns",
        N,
        {"visual_field.abnormal": True, "visual_field.abnormal_has_concerns": True},
        "IN",
    ),
    (
        "VF abnormal + BIG BOX YES",
        N,
        {
            "visual_field.abnormal": True,
            "visual_field.meet_criteria_for_licence_class_yes": True,
        },
        "CP",
    ),
    (
        "VF abnormal + concerns + BIG BOX YES + monocular (dated old)",
        N,
        {
            "visual_field.abnormal": True,
            "visual_field.abnormal_has_concerns": True,
            "visual_field.meet_criteria_for_licence_class_yes": True,
            "vision.monocular": True,
            "vision.monocular_date": "2024-01-01",
        },
        "IN",
    ),
    (
        "VF abnormal (no VF concerns) + monocular dated old -> monocular rules",
        N,
        {
            "visual_field.abnormal": True,
            "vision.monocular": True,
            "vision.monocular_date": "2024-01-01",
        },
        "CP",
    ),
    ("nothing provided, BIG BOX empty", N, {}, "IN"),
    (
        "no acuity, VF Normal + BIG BOX YES",
        N,
        {
            "visual_field.normal": True,
            "visual_field.meet_criteria_for_licence_class_yes": True,
        },
        "CP",
    ),
    (
        "no acuity, only BIG BOX YES",
        N,
        {"visual_field.meet_criteria_for_licence_class_yes": True},
        "CP",
    ),
    (
        "Class 5-8 corrected 20/40, VF normal",
        N,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/40"},
        "CP",
    ),
    (
        "Class 5-8 corrected 20/70",
        N,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/70"},
        "IN",
    ),
    (
        "Class 5-8 corrected 20/80",
        N,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/80"},
        "CR",
    ),
    (
        "Class 5-8 uncorrected-only 20/60",
        N,
        {**VF_NORMAL, "visual_acuity.uncorrected_both": "20/60"},
        "IN",
    ),
    (
        "Class 5-8 superscript 20/50cf",
        N,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/50cf"},
        "CP",
    ),
    (
        "High corrected 20/30, VF normal",
        H,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/30"},
        "CP",
    ),
    (
        "High corrected 20/40",
        H,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/40"},
        "IN",
    ),
    (
        "High corrected 20/60",
        H,
        {**VF_NORMAL, "visual_acuity.corrected_both": "20/60"},
        "CR",
    ),
    (
        "Monocular Class 5-8, dated >3m, with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
            "vision.monocular_date": ago(months=6),
        },
        "CP",
    ),
    (
        "Monocular Class 5-8, dated <3m, no concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_date": ago(months=1),
        },
        "CP",
    ),
    (
        "Monocular Class 5-8, dated <3m, with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
            "vision.monocular_date": ago(months=1),
        },
        "PU",
    ),
    (
        "Monocular Class 5-8, undated, DMER <3m, with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
        },
        "IN",
    ),
    (
        "Monocular Class 5-8, undated, DMER >3m, with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
            "received_date": ago(months=5),
        },
        "CP",
    ),
    (
        "Monocular High, dated >12m, no concerns",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_date": ago(months=18),
        },
        "CP",
    ),
    (
        "Monocular High, dated >12m, with concerns",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
            "vision.monocular_date": ago(months=18),
        },
        "PU",
    ),
    (
        "Monocular High, dated <12m, no concerns",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_date": ago(months=6),
        },
        "PU",
    ),
    (
        "Monocular High, dated <12m, with concerns",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "vision.monocular_has_concerns": True,
            "vision.monocular_date": ago(months=6),
        },
        "CR",
    ),
    (
        "Monocular High, undated, DMER <12m",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
        },
        "IN",
    ),
    (
        "Monocular High, undated, DMER >12m, no concerns",
        H,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.monocular": True,
            "received_date": ago(months=14),
        },
        "CP",
    ),
    (
        "Monocular via bad eye 20/200 (auto-flagged), Class 5-8 undated DMER <3m",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_left": "20/20",
            "visual_acuity.corrected_right": "20/200",
        },
        "CP",
    ),
    (
        "Progressive Class 5-8 corrected 20/40, fields meet",
        N,
        {
            **VF_NORMAL,
            "vision.cataracts": True,
            "visual_acuity.corrected_both": "20/40",
        },
        "IN",
    ),
    (
        "Progressive Class 5-8 corrected 20/25, fields meet",
        N,
        {
            **VF_NORMAL,
            "vision.retinopathy": True,
            "visual_acuity.corrected_both": "20/25",
        },
        "CP",
    ),
    (
        "Progressive Class 5-8 uncorrected-only 20/40",
        N,
        {
            **VF_NORMAL,
            "vision.progressive_eye_condition": True,
            "visual_acuity.uncorrected_both": "20/40",
        },
        "CP",
    ),
    (
        "Progressive High corrected 20/30",
        H,
        {
            **VF_NORMAL,
            "vision.progressive_eye_condition": True,
            "visual_acuity.corrected_both": "20/30",
        },
        "IN",
    ),
    (
        "Progressive High corrected 20/20",
        H,
        {
            **VF_NORMAL,
            "vision.progressive_eye_condition": True,
            "visual_acuity.corrected_both": "20/20",
        },
        "CP",
    ),
    (
        "Retinal detachment, acuity + fields meet",
        N,
        {
            **VF_NORMAL,
            "vision.retinal_detachment": True,
            "visual_acuity.corrected_both": "20/30",
        },
        "CP",
    ),
    (
        "Retinal detachment, acuity fails -> VA rules",
        N,
        {
            **VF_NORMAL,
            "vision.retinal_detachment": True,
            "visual_acuity.corrected_both": "20/70",
        },
        "IN",
    ),
    (
        "Eye nerve palsy with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.eye_nerve_palsy": True,
            "vision.eye_nerve_palsy_has_concerns": True,
        },
        "IN",
    ),
    (
        "Diplopia with concerns",
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "vision.diplopia": True,
            "vision.diplopia_has_concerns": True,
        },
        "IN",
    ),
]


@pytest.mark.parametrize(
    ("name", "licence", "fields", "expected"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
def test_vision_scenario(name, licence, fields, expected):
    assert vision_action(evaluate(licence, fields)) == expected


# ---------------------------------------------------------------- rows (does the named row fire?)

ROWS = [  # (label, table, row, licence, fields, expected)
    (
        "HbA1C 12.5 (decimal), no concerns",
        "Diabetes",
        10,
        N,
        {"endocrine.HbA1C": 12.5},
        True,
    ),
    (
        "diabetes insipidus",
        "Diabetes",
        9,
        N,
        {"endocrine.diabetes_insipidus": True},
        True,
    ),
    (
        "glucose tolerance",
        "Diabetes",
        9,
        N,
        {"endocrine.glucose_tolerance": True},
        True,
    ),
    (
        "diabetes on oral meds, AGE DMER",
        "Diabetes",
        3,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "type": "age",
        },
        True,
    ),
    (
        "diabetes, no treatment, AGE DMER, stable",
        "Diabetes",
        1,
        N,
        {
            "endocrine.diabetes": True,
            "type": "age",
            "endocrine.stable_bg_control": True,
        },
        True,
    ),
    (
        "cardiomyopathy, LVEF 30",
        "Cardiovascular",
        6,
        N,
        {"cardiovascular.cardiomyopathy": True, "cardiovascular.lvef": 30},
        True,
    ),
    (
        "ventricular tachycardia",
        "Cardiovascular",
        65,
        N,
        {"cardiovascular.ventricular_tachycardia": True},
        True,
    ),
    (
        "ventricular fibrillation",
        "Cardiovascular",
        65,
        N,
        {"cardiovascular.ventricular_fibrillation": True},
        True,
    ),
    (
        "cardiac transplant 2 months ago, no NYHA/LVEF",
        "Cardiovascular",
        62,
        H,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(months=2),
        },
        True,
    ),
    (
        "renal failure, no concerns",
        "Chronic Renal",
        7,
        N,
        {"chronicrenal.renal_failure": True},
        True,
    ),
    (
        "subdural hematoma, no deficit, no concern",
        "Cerebrovascular",
        17,
        N,
        {"cerebrovascular.subdural_hematoma": True},
        True,
    ),
    (
        "subdural hematoma with concern",
        "Cerebrovascular",
        19,
        N,
        {
            "cerebrovascular.subdural_hematoma": True,
            "cerebrovascular.subdural_hematoma_has_concern": True,
        },
        True,
    ),
    (
        "blood disorder",
        "General Debility",
        8,
        N,
        {"general.blood_disorders": True},
        True,
    ),
    (
        "Christian Science exemption",
        "General Debility",
        9,
        N,
        {"general.christian_science_exemption": True},
        True,
    ),
    (
        "guide 17 + opinion no + another condition",
        "General Debility",
        2,
        N,
        {
            "guide": "BC GUIDE 17 GENERAL DEBILITY",
            "opinion.no": True,
            "general.general_debility": True,
        },
        True,
    ),
    (
        "guide present, nothing else on the DMER",
        "Priority",
        10,
        N,
        {"guide": "BC GUIDE 17 GENERAL DEBILITY"},
        True,
    ),
    (
        "hearing loss + tinnitus, High, no concerns",
        "Hearing Loss",
        5,
        H,
        {"hearing.hearing_loss": True, "hearing.tinnitus": True},
        True,
    ),
    (
        "other psych diagnosis, stable/compliant",
        "Psychiatric Disorders",
        23,
        N,
        {
            "psychiatric.other_psych_diagnosis": True,
            "psychiatric.stable_condition": True,
            "psychiatric.compliant_with_treatment": True,
        },
        True,
    ),
    (
        "no other psych diagnosis",
        "Psychiatric Disorders",
        23,
        N,
        {
            "psychiatric.stable_condition": True,
            "psychiatric.compliant_with_treatment": True,
        },
        False,
    ),
    (
        "cognitive impairment, Trails B 200s, no other scores",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_b_seconds": 200},
        True,
    ),
    (
        "cognitive impairment, Trails B 200s, other scores all normal",
        "CognitiveImpairment",
        9,
        N,
        {
            "cns.cognitiveimpairment": True,
            "cns.trails_b_seconds": 200,
            "cns.mmse_score": 28,
            "cns.moca_score": 28,
            "cns.simard_score": 90,
            "cns.gds_level_score": 1,
            "cns.trails_a_seconds": 40,
        },
        True,
    ),
    (
        "road test recommended, Trails B 120s only",
        "CognitiveImpairment",
        13,
        N,
        {"recommendations.road_test_to_assess": True, "cns.trails_b_seconds": 120},
        True,
    ),
    (
        "cognitive impairment, MMSE 12 only",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.mmse_score": 12},
        True,
    ),
    (
        "severe impairment, no scores at all",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.cognitiveimpairment_is_severe": True},
        True,
    ),
    (
        "severe impairment but Trails B recorded (80s)",
        "CognitiveImpairment",
        9,
        N,
        {
            "cns.cognitiveimpairment": True,
            "cns.cognitiveimpairment_is_severe": True,
            "cns.trails_b_seconds": 80,
        },
        False,
    ),
    (
        "road test recommended, MoCA 28 only",
        "CognitiveImpairment",
        13,
        N,
        {"recommendations.road_test_to_assess": True, "cns.moca_score": 28},
        True,
    ),
    (
        "CVA, no date, with concern",
        "Cerebrovascular",
        13,
        N,
        {"cerebrovascular.cva": True, "cerebrovascular.cva_tia_has_concern": True},
        True,
    ),
    (
        "TIA, no date, no concern, High",
        "Cerebrovascular",
        16,
        H,
        {"cerebrovascular.tia": True},
        True,
    ),
    (
        "pacemaker, no date, non-AGE DMER",
        "Cardiovascular",
        67,
        N,
        {"type": "other", "cardiovascular.pacemaker": True},
        True,
    ),
    (
        "psychosis, no date, stable, concerns -> R17 IN no longer (PS1)",
        "Psychiatric Disorders",
        17,
        N,
        {
            "psychiatric.psychosis": True,
            "psychiatric.stable_condition": True,
            "psychiatric.psychosis_has_concerns": True,
        },
        False,
    ),
    ("epilepsy, no seizure date", "Seizures", 3, N, {"cns.epilepsy": True}, True),
    (
        "recurrent vertigo, no date, no warnings box -> R10 PR",
        "Vestibular Disorders",
        10,
        N,
        {"vestibular.recurrent_vertigo": True},
        True,
    ),
    (
        "cardiac transplant 8 months ago -> R62 does not fire (was firing via cad_date)",
        "Cardiovascular",
        62,
        H,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(months=8),
        },
        False,
    ),
    (
        "diabetes on insulin, AGE DMER, stable, compliant",
        "Diabetes",
        2,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
            "endocrine.stable_bg_control": True,
        },
        True,
    ),
    (
        "diabetes on insulin, non-AGE DMER -> R8 fallback",
        "Diabetes",
        8,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.insulin": True,
            "type": "other",
        },
        True,
    ),
    (
        "guide 9.1, AGE, insulin ticked, stable, compliant",
        "Diabetes",
        1,
        N,
        {
            "guide": "9.1",
            "type": "age",
            "endocrine.diabetes.insulin": True,
            "endocrine.stable_bg_control": True,
        },
        True,
    ),
    (
        "guide 9.1A, HIGH -> fallback IN",
        "Diabetes",
        8,
        H,
        {"guide": "9.1A", "type": "age"},
        True,
    ),
    (
        "guide 9.1, AGE, NORMAL, stable -> R8 does not also fire",
        "Diabetes",
        8,
        N,
        {"guide": "9.1", "type": "age", "endocrine.stable_bg_control": True},
        False,
    ),
    (
        "D4: HIGH, insulin, AGE, no R22 -> fallback",
        "Diabetes",
        8,
        H,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
            "endocrine.stable_bg_control": True,
        },
        True,
    ),
    (
        "D6: NORMAL, insulin, AGE, non-compliant -> fallback",
        "Diabetes",
        8,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
            "endocrine.stable_bg_control": True,
            "endocrine.diabetes.non_compliant": True,
        },
        True,
    ),
    (
        "D2: HIGH, oral meds, non-AGE, no concerns -> R8 does not fire",
        "Diabetes",
        8,
        H,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "type": "other",
        },
        False,
    ),
    (
        "HIGH, AGE, oral meds + insulin, no R22, no concerns -> R4 CP also fires",
        "Diabetes",
        4,
        H,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
        },
        True,
    ),
    (
        "HIGH, AGE, oral meds + insulin, no R22, no concerns -> fallback IN",
        "Diabetes",
        8,
        H,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
        },
        True,
    ),
    (
        "Section C Yes + hypertension -> no CR",
        "Priority",
        2,
        N,
        {"opinion.yes": True, "cardiovascular.hypertension": True},
        False,
    ),
    (
        "Section C Yes + hypertension -> no PU fallback",
        "Priority",
        5,
        N,
        {"opinion.yes": True, "cardiovascular.hypertension": True},
        False,
    ),
    (
        "Section C Yes + aortic stenosis w/ concerns -> no PU fallback",
        "Priority",
        5,
        N,
        {
            "opinion.yes": True,
            "cardiovascular.aortic_stenosis": True,
            "cardiovascular.aortic_stenosis_has_concerns": True,
        },
        False,
    ),
    (
        "Section C Yes, nothing else -> PU fallback",
        "Priority",
        5,
        N,
        {"opinion.yes": True},
        True,
    ),
    (
        "physician has concerns about driving -> PU",
        "Priority",
        5,
        N,
        {"priority.has_concerns": True},
        True,
    ),
    (
        "should not drive, no reason, nothing else -> IN",
        "Priority",
        1,
        N,
        {"priority.should_not_drive": True},
        True,
    ),
    (
        "should not drive, with reason -> CR",
        "Priority",
        2,
        N,
        {
            "priority.should_not_drive": True,
            "priority.should_not_drive_has_reason": True,
        },
        True,
    ),
    (
        "road test box only -> IN",
        "Priority",
        6,
        N,
        {"recommendations.road_test_to_assess": True},
        True,
    ),
    (
        "guide 17, Section C No, nothing else -> IN",
        "General Debility",
        3,
        N,
        {"guide": "BC GUIDE 17 GENERAL DEBILITY", "opinion.no": True},
        True,
    ),
    (
        "SZ1 seizure 2 years ago -> R2 IN does not fire",
        "Seizures",
        2,
        N,
        {"cns.epilepsy": True, "cns.seizure_date": ago(years=2)},
        False,
    ),
    (
        "SZ1 seizure 2 years ago -> R1 CP",
        "Seizures",
        1,
        N,
        {"cns.epilepsy": True, "cns.seizure_date": ago(years=2)},
        True,
    ),
    (
        "SZ1 seizure 8 months ago -> R2 IN",
        "Seizures",
        2,
        N,
        {"cns.epilepsy": True, "cns.seizure_date": ago(months=8)},
        True,
    ),
    (
        "SZ2 HIGH, R22, no seizure date -> R5 PU",
        "Seizures",
        5,
        H,
        {"cns.epilepsy": True, "restrictions": [22]},
        True,
    ),
    (
        "G2 seizure 4 months before an exam 8 months ago -> R3 PU (measured from exam)",
        "Seizures",
        3,
        N,
        {
            "cns.epilepsy": True,
            "cns.seizure_date": ago(months=12),
            "medical_examination_date": ago(months=8),
        },
        True,
    ),
    (
        "G2 same seizure -> R2 IN does not fire",
        "Seizures",
        2,
        N,
        {
            "cns.epilepsy": True,
            "cns.seizure_date": ago(months=12),
            "medical_examination_date": ago(months=8),
        },
        False,
    ),
    (
        "C1 NORMAL arrhythmia, non-AGE, Section D text, no concerns -> R44 IN",
        "Cardiovascular",
        44,
        N,
        {
            "type": "other",
            "cardiovascular.arrhythmia": True,
            "details_of_condition": "atrial fibrillation, on metoprolol",
        },
        True,
    ),
    (
        "C1 HIGH arrhythmia with syncope -> R45 CP does not fire",
        "Cardiovascular",
        45,
        H,
        {"cardiovascular.arrhythmia": True, "cardiovascular.syncope": True},
        False,
    ),
    (
        "C2 arrhythmia with LOC -> R46 PR",
        "Cardiovascular",
        46,
        N,
        {"cardiovascular.arrhythmia": True, "cardiovascular.loc": True},
        True,
    ),
    (
        "C3 HIGH CAD with concerns, NYHA 1, a year ago -> R51 PR",
        "Cardiovascular",
        51,
        H,
        {
            "cardiovascular.cad": True,
            "cardiovascular.cad_has_concerns": True,
            "cardiovascular.nyha_class": 1,
            "cardiovascular.cad_date": ago(years=1),
        },
        True,
    ),
    (
        "C4 HIGH CHF NYHA 2 no concerns -> R72 CP",
        "Cardiovascular",
        72,
        H,
        {
            "cardiovascular.congestive_heart_failure": True,
            "cardiovascular.nyha_class": 2,
        },
        True,
    ),
    (
        "C5 NORMAL transplant, no date -> R73 PR",
        "Cardiovascular",
        73,
        N,
        {"cardiovascular.cardiac_transplant": True},
        True,
    ),
    (
        "CV1 aneurysm repaired 2y ago -> R3 'no info' PR does not fire",
        "Cerebrovascular",
        3,
        N,
        {
            "cerebrovascular.cerebral_aneurysm": True,
            "cerebrovascular.cerebral_aneurysm_repaired": True,
            "cerebrovascular.cerebral_aneurysm_date": ago(years=2),
        },
        False,
    ),
    (
        "CV2 aneurysm repaired 2y ago -> R5 PR does not fire",
        "Cerebrovascular",
        5,
        N,
        {
            "cerebrovascular.cerebral_aneurysm": True,
            "cerebrovascular.cerebral_aneurysm_repaired": True,
            "cerebrovascular.cerebral_aneurysm_date": ago(years=2),
        },
        False,
    ),
    (
        "CV2 aneurysm repaired 1 month before exam -> R5 PR",
        "Cerebrovascular",
        5,
        N,
        {
            "cerebrovascular.cerebral_aneurysm": True,
            "cerebrovascular.cerebral_aneurysm_repaired": True,
            "cerebrovascular.cerebral_aneurysm_date": ago(months=1, days=10),
        },
        True,
    ),
    (
        "CV1 aneurysm, nothing else -> R3 PR",
        "Cerebrovascular",
        3,
        N,
        {"cerebrovascular.cerebral_aneurysm": True},
        True,
    ),
    (
        "CG1 no diagnosis, not s.7/15, scores normal -> R1 CP",
        "CognitiveImpairment",
        1,
        N,
        {"cns.mmse_score": 28, "cns.moca_score": 28},
        True,
    ),
    (
        "CG2 impairment, Trails B 120s, other scores normal -> R7 PU",
        "CognitiveImpairment",
        7,
        N,
        {
            "cns.cognitiveimpairment": True,
            "cns.mmse_score": 28,
            "cns.moca_score": 28,
            "cns.simard_score": 90,
            "cns.gds_level_score": 1,
            "cns.trails_a_seconds": 100,
            "cns.trails_b_seconds": 120,
        },
        True,
    ),
    (
        "CG4 impairment noted mild, no scores -> R11 PR",
        "CognitiveImpairment",
        11,
        N,
        {
            "cns.cognitiveimpairment": True,
            "cns.cognitiveimpairment_is_mild_or_moderate": True,
        },
        True,
    ),
    (
        "CG4 impairment, severity not stated, no scores -> R11 does not fire",
        "CognitiveImpairment",
        11,
        N,
        {"cns.cognitiveimpairment": True},
        False,
    ),
    (
        "H1 HIGH hearing loss, passed whisper, no R23 -> R9 IN does not fire",
        "Hearing Loss",
        9,
        H,
        {"hearing.hearing_loss": True, "hearing.passed_whisper_test": True},
        False,
    ),
    (
        "H1 HIGH hearing loss only, no R23 -> R9 IN",
        "Hearing Loss",
        9,
        H,
        {"hearing.hearing_loss": True},
        True,
    ),
    (
        "IT1 HIGH tumor, OK to drive -> R4 PR",
        "Intracranial Tumors",
        4,
        H,
        {"cns.intracranial_tumors": True, "opinion.no": True},
        True,
    ),
    (
        "IT1 HIGH tumor -> R2 IN does not fire",
        "Intracranial Tumors",
        2,
        H,
        {"cns.intracranial_tumors": True, "opinion.no": True},
        False,
    ),
    (
        "IT2 NORMAL resected tumor, OK to drive -> R2 IN does not fire",
        "Intracranial Tumors",
        2,
        N,
        {
            "cns.intracranial_tumors": True,
            "cns.intracranial_tumors.resected": True,
            "opinion.no": True,
        },
        False,
    ),
    (
        "M1 finger amputation, no restrictions -> R2 PR does not fire",
        "Musculoskeletal",
        2,
        N,
        {
            "musculoskeletal.limb_amputation": True,
            "musculoskeletal.limb_amputation_fingers_or_toes": True,
        },
        False,
    ),
    (
        "M3 amputation 1 year before exam -> R3 TCM",
        "Musculoskeletal",
        3,
        N,
        {
            "musculoskeletal.limb_amputation": True,
            "musculoskeletal.limb_amputation_date": ago(years=1),
        },
        True,
    ),
    (
        "M6 polio only -> R15 CP",
        "Musculoskeletal",
        15,
        N,
        {"musculoskeletal.polio": True},
        True,
    ),
    (
        "P2 aortic dissection, no concerns -> R7 PR does not fire",
        "Peripheral Vascular Diseases",
        7,
        N,
        {"pvd.aortic_dissection": True},
        False,
    ),
    (
        "PS1 psychosis, no date, stable, no concerns -> R17 IN",
        "Psychiatric Disorders",
        17,
        N,
        {"psychiatric.psychosis": True, "psychiatric.stable_condition": True},
        True,
    ),
    (
        "PS2 psychosis with concerns -> R25 PU",
        "Psychiatric Disorders",
        25,
        N,
        {"psychiatric.psychosis": True, "psychiatric.psychosis_has_concerns": True},
        True,
    ),
    (
        "SL1 severe OSA, no CPAP info -> R5 PU",
        "Sleep Disorders",
        5,
        N,
        {"sleep.obstructive_sleep_apnea": True, "sleep.osa_severe": True},
        True,
    ),
    (
        "V1 drop attacks, no date -> R7 PR",
        "Vestibular Disorders",
        7,
        N,
        {"vestibular.drop_attacks": True},
        True,
    ),
    (
        "DB1 HIGH HbA1C 13 -> R11 (now IN)",
        "Diabetes",
        11,
        H,
        {"endocrine.HbA1C": 13},
        True,
    ),
    (
        "1 HIGH transplant 8 months ago, no concerns -> R61 IN",
        "Cardiovascular",
        61,
        H,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(months=8),
        },
        True,
    ),
    (
        "2 HIGH transplant 3 years ago, no NYHA/LVEF -> R62 does not fire",
        "Cardiovascular",
        62,
        H,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(years=3),
        },
        False,
    ),
    (
        "2 HIGH transplant, no date, no NYHA/LVEF -> R62 PR",
        "Cardiovascular",
        62,
        H,
        {"cardiovascular.cardiac_transplant": True},
        True,
    ),
    (
        "3 NORMAL transplant NYHA 2 -> R63 does not fire",
        "Cardiovascular",
        63,
        N,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(years=3),
            "cardiovascular.nyha_class": 2,
        },
        False,
    ),
    (
        "3 HIGH transplant NYHA 2 -> R63 PU",
        "Cardiovascular",
        63,
        H,
        {
            "cardiovascular.cardiac_transplant": True,
            "cardiovascular.cardiac_transplant_date": ago(years=3),
            "cardiovascular.nyha_class": 2,
        },
        True,
    ),
    (
        "4 NORMAL aortic stenosis, no concerns -> R15 IN",
        "Cardiovascular",
        15,
        N,
        {"cardiovascular.aortic_stenosis": True},
        True,
    ),
    (
        "4 NORMAL aortic stenosis, concerns -> R15 does not fire",
        "Cardiovascular",
        15,
        N,
        {
            "cardiovascular.aortic_stenosis": True,
            "cardiovascular.aortic_stenosis_has_concerns": True,
        },
        False,
    ),
    (
        "4 HIGH cardiomyopathy, no concerns -> R41 CP",
        "Cardiovascular",
        41,
        H,
        {"cardiovascular.cardiomyopathy": True},
        True,
    ),
    (
        "5 no diagnosis, MoCA 28 only -> R1 CP",
        "CognitiveImpairment",
        1,
        N,
        {"cns.moca_score": 28},
        True,
    ),
    (
        "5 no diagnosis, Trails B 100s only -> R3 CP",
        "CognitiveImpairment",
        3,
        N,
        {"cns.trails_b_seconds": 100},
        True,
    ),
    (
        "6 impairment, MoCA 20 only -> R7 PU",
        "CognitiveImpairment",
        7,
        N,
        {"cns.cognitiveimpairment": True, "cns.moca_score": 20},
        True,
    ),
    (
        "7 HIGH severe impairment, no scores -> R10 CR",
        "CognitiveImpairment",
        10,
        H,
        {"cns.cognitiveimpairment": True, "cns.cognitiveimpairment_is_severe": True},
        True,
    ),
    (
        "8 HIGH road test, MoCA 28 only -> R14 PR",
        "CognitiveImpairment",
        14,
        H,
        {"recommendations.road_test_to_assess": True, "cns.moca_score": 28},
        True,
    ),
    (
        "9 impairment, MoCA 20 only -> R5 'no score' IN does not fire",
        "CognitiveImpairment",
        5,
        N,
        {"cns.cognitiveimpairment": True, "cns.moca_score": 20},
        False,
    ),
    (
        "9 impairment, no scores -> R5 IN",
        "CognitiveImpairment",
        5,
        N,
        {"cns.cognitiveimpairment": True},
        True,
    ),
    (
        "10 AAA, no size, concerns -> R4 PU",
        "Peripheral Vascular Diseases",
        4,
        N,
        {
            "pvd.abdominal_aortic_aneurysm": True,
            "pvd.abdominal_aortic_aneurysm_has_concerns": True,
        },
        True,
    ),
    (
        "10 AAA, no size, no concerns -> R1 IN",
        "Peripheral Vascular Diseases",
        1,
        N,
        {"pvd.abdominal_aortic_aneurysm": True},
        True,
    ),
    (
        "11 HIGH hearing loss, right 30 dB, left blank -> R3 CP",
        "Hearing Loss",
        3,
        H,
        {"hearing.hearing_loss": True, "hearing.hearing_db_right": 30},
        True,
    ),
    (
        "12 OSA, AHI 10, no concerns, no sleepiness -> R3 CP",
        "Sleep Disorders",
        3,
        N,
        {
            "sleep.obstructive_sleep_apnea": True,
            "sleep.ahi_score": 10,
            "sleep.no_daytime_sleepiness": True,
        },
        True,
    ),
    (
        "12 OSA, AHI 40 -> R6 PU",
        "Sleep Disorders",
        6,
        N,
        {"sleep.obstructive_sleep_apnea": True, "sleep.ahi_score": 40},
        True,
    ),
    (
        "13 vertigo without warnings, 2y ago, no concerns -> R5 CP",
        "Vestibular Disorders",
        5,
        N,
        {
            "vestibular.recurrent_vertigo": True,
            "vestibular.vertigo_without_warnings": True,
            "vestibular.vertigo_date": ago(years=2),
        },
        True,
    ),
    (
        "13 vertigo without warnings, 2y ago, no concerns -> R6 does not fire",
        "Vestibular Disorders",
        6,
        N,
        {
            "vestibular.recurrent_vertigo": True,
            "vestibular.vertigo_without_warnings": True,
            "vestibular.vertigo_date": ago(years=2),
        },
        False,
    ),
    (
        "13 vertigo without warnings, no date -> R6 PR",
        "Vestibular Disorders",
        6,
        N,
        {
            "vestibular.recurrent_vertigo": True,
            "vestibular.vertigo_without_warnings": True,
        },
        True,
    ),
    (
        "13 vertigo with warnings, no concerns -> R8 CP",
        "Vestibular Disorders",
        8,
        N,
        {
            "vestibular.recurrent_vertigo": True,
            "vestibular.vertigo_with_warnings": True,
        },
        True,
    ),
    (
        "13 vertigo with warnings, 2 months ago -> R9 PR",
        "Vestibular Disorders",
        9,
        N,
        {
            "vestibular.recurrent_vertigo": True,
            "vestibular.vertigo_with_warnings": True,
            "vestibular.vertigo_date": ago(months=2),
        },
        True,
    ),
    (
        "13 vertigo, neither warnings box -> R10 PR",
        "Vestibular Disorders",
        10,
        N,
        {"vestibular.recurrent_vertigo": True},
        True,
    ),
    (
        "14 on psych medications, compliant -> Psychotropic R1 CP",
        "Psychotropic Drugs",
        1,
        N,
        {
            "psychiatric.on_medications": True,
            "psychiatric.compliant_with_treatment": True,
        },
        True,
    ),
    (
        "15 abuse/dependence, 'maybe', follow-up 3 years, concerns -> R3 CP",
        "Psychotropic Drugs",
        3,
        N,
        {
            "psychotropic_drugs.substance_abuse_or_dependence": True,
            "psychotropic_drugs.substance_use_has_concerns": True,
            "opinion.maybe": True,
            "opinion.maybe_followup_years": 3,
        },
        True,
    ),
    (
        "15 abuse/dependence, 'maybe', follow-up blank -> R4 does not fire (no error)",
        "Psychotropic Drugs",
        4,
        N,
        {
            "psychotropic_drugs.substance_abuse_or_dependence": True,
            "opinion.maybe": True,
        },
        False,
    ),
    (
        "AGE arrhythmia, no concerns -> R44 IN does not fire",
        "Cardiovascular",
        44,
        N,
        {"type": "age", "cardiovascular.arrhythmia": True},
        False,
    ),
    (
        "AGE CAD 2y ago, no concerns -> R48 IN does not fire",
        "Cardiovascular",
        48,
        N,
        {
            "type": "age",
            "cardiovascular.cad": True,
            "cardiovascular.cad_date": ago(years=2),
        },
        False,
    ),
    (
        "AGE CHF NYHA 1, no concerns -> R53 IN does not fire",
        "Cardiovascular",
        53,
        N,
        {
            "type": "age",
            "cardiovascular.congestive_heart_failure": True,
            "cardiovascular.nyha_class": 1,
        },
        False,
    ),
    (
        "AGE pacemaker 2y ago, no concerns -> R67 IN does not fire",
        "Cardiovascular",
        67,
        N,
        {
            "type": "age",
            "cardiovascular.pacemaker": True,
            "cardiovascular.pacemaker_date": ago(years=2),
        },
        False,
    ),
    (
        "AGE pacemaker 2y ago, no concerns -> R66 CP",
        "Cardiovascular",
        66,
        N,
        {
            "type": "age",
            "cardiovascular.pacemaker": True,
            "cardiovascular.pacemaker_date": ago(years=2),
        },
        True,
    ),
    (
        "non-AGE pacemaker 2y ago, no concerns -> R67 IN",
        "Cardiovascular",
        67,
        N,
        {
            "type": "other",
            "cardiovascular.pacemaker": True,
            "cardiovascular.pacemaker_date": ago(years=2),
        },
        True,
    ),
    (
        "pulmonary embolism, not resolved -> R9 CP",
        "Respiratory Diseases",
        9,
        N,
        {"respiratory.pulmonary_embolism": True},
        True,
    ),
    (
        "abuse/dependence within 2y, no concerns -> Psychotropic R3 CP",
        "Psychotropic Drugs",
        3,
        N,
        {"psychotropic_drugs.substance_abuse_or_dependence": True},
        True,
    ),
    (
        "abuse/dependence within 2y, concerns -> Psychotropic R4 PR",
        "Psychotropic Drugs",
        4,
        N,
        {
            "psychotropic_drugs.substance_abuse_or_dependence": True,
            "psychotropic_drugs.substance_use_has_concerns": True,
        },
        True,
    ),
    (
        "SUD ACTIVE -> Psychotropic R5 PU",
        "Psychotropic Drugs",
        5,
        N,
        {"psychotropic_drugs.substance_use": True},
        True,
    ),
    (
        "AUD ACTIVE, compliant with treatment -> R6 PR",
        "Psychotropic Drugs",
        6,
        N,
        {
            "psychotropic_drugs.alcohol_use": True,
            "psychotropic_drugs.perscribed_drugs_compliant": True,
        },
        True,
    ),
    (
        "AUD ACTIVE, compliant with treatment -> R5 PU does not fire",
        "Psychotropic Drugs",
        5,
        N,
        {
            "psychotropic_drugs.alcohol_use": True,
            "psychotropic_drugs.perscribed_drugs_compliant": True,
        },
        False,
    ),
    (
        "AUD ACTIVE -> R3 (abuse row) does not fire",
        "Psychotropic Drugs",
        3,
        N,
        {"psychotropic_drugs.alcohol_use": True},
        False,
    ),
    (
        "bipolar, no stability/compliance info -> catch-all IN",
        "Priority",
        11,
        N,
        {"psychiatric.bipolar": True},
        True,
    ),
    (
        "syncope, no date, no concerns -> catch-all IN",
        "Priority",
        11,
        N,
        {"cardiovascular.syncope": True},
        True,
    ),
    (
        "non-AAA aneurysm -> catch-all IN",
        "Priority",
        11,
        N,
        {"pvd.aneurysm": True},
        True,
    ),
    (
        "hypertension only -> catch-all does not fire",
        "Priority",
        11,
        N,
        {"cardiovascular.hypertension": True},
        False,
    ),
    (
        "colour blindness only -> catch-all does not fire",
        "Priority",
        11,
        N,
        {"vision.color_blindness": True},
        False,
    ),
    ("nothing marked -> catch-all does not fire", "Priority", 11, N, {}, False),
    (
        "persistent hypo unawareness, NORMAL, no date -> TCM",
        "Diabetes",
        17,
        N,
        {"endocrine.persistent_hypoglycemic_unawareness": True},
        True,
    ),
    (
        "persistent hypo unawareness, NORMAL, 2 months ago -> CR",
        "Diabetes",
        18,
        N,
        {
            "endocrine.persistent_hypoglycemic_unawareness": True,
            "endocrine.persistent_hypoglycemic_unawareness_date": ago(months=2),
        },
        True,
    ),
    (
        "persistent hypo unawareness, NORMAL, 1 year ago -> no TCM",
        "Diabetes",
        17,
        N,
        {
            "endocrine.persistent_hypoglycemic_unawareness": True,
            "endocrine.persistent_hypoglycemic_unawareness_date": ago(years=1),
        },
        False,
    ),
    (
        "persistent hypo unawareness, NORMAL, 1 year ago -> no CR",
        "Diabetes",
        18,
        N,
        {
            "endocrine.persistent_hypoglycemic_unawareness": True,
            "endocrine.persistent_hypoglycemic_unawareness_date": ago(years=1),
        },
        False,
    ),
    (
        "persistent hypo unawareness, HIGH -> CR",
        "Diabetes",
        19,
        H,
        {"endocrine.persistent_hypoglycemic_unawareness": True},
        True,
    ),
    (
        "diabetes on insulin, AGE DMER -> R8 does not fire",
        "Diabetes",
        8,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.insulin": True,
            "type": "age",
            "endocrine.stable_bg_control": True,
        },
        False,
    ),
    (
        "diabetes + HbA1C 13 + severe hypo 1 month ago: CR row also collected",
        "Diabetes",
        16,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.diet": True,
            "endocrine.severe_hypoglycemia": True,
            "endocrine.severe_hypoglycemia_date": ago(months=1),
        },
        True,
    ),
    (
        "restriction '20' makes a Class 5 licence HIGH (corrected 20/40 -> IN)",
        None,
        None,
        "5",
        {
            "restrictions": "20",
            "visual_field.normal": True,
            "visual_acuity.corrected_both": "20/40",
        },
        "IN",
    ),
]


@pytest.mark.parametrize(
    ("label", "table", "row", "licence", "fields", "expected"),
    ROWS,
    ids=[r[0] for r in ROWS],
)
def test_row(label, table, row, licence, fields, expected):
    evaluation = evaluate(licence, fields)
    if table is None:
        assert vision_action(evaluation) == expected
    else:
        assert row_fired(evaluation, table, row) is expected


# ---------------------------------------------------------------- outcome selection


def _o(
    action: str, *, fit: bool = False, reason: str = "r", record: bool = False
) -> Outcome:
    return Outcome(
        section="s",
        action=action,
        fit_letter=fit,
        reason=reason,
        driving_record_check=record,
    )


def test_highest_priority_action_wins_and_reasons_at_that_level_combine():
    evaluation = select_outcome(
        (_o("CP", reason="a"), _o("PU", reason="b", fit=True), _o("PU", reason="c"))
    )
    assert evaluation.selected_outcome_code == "PU"
    assert evaluation.rule_engine_outcome_code == "PU"
    assert evaluation.selected_reason == "b, and c"
    assert evaluation.fit_letter is True
    assert evaluation.priority_rank == OUTCOME_PRIORITY.index("PU")


def test_clean_pass_with_a_driving_record_row_goes_to_intake():
    evaluation = select_outcome(
        (_o("CP", reason="meds", record=True), _o("CP", reason="vision"))
    )
    assert evaluation.rule_engine_outcome_code == "CP"
    assert evaluation.selected_outcome_code == "IN"
    assert "driving record" in evaluation.selected_reason


def test_driving_record_row_does_not_lower_a_higher_outcome():
    assert (
        select_outcome((_o("PR", record=True), _o("CP"))).selected_outcome_code == "PR"
    )


def test_no_outcomes_goes_to_intake():
    evaluation = select_outcome(())
    assert evaluation.selected_outcome_code == "IN"
    assert evaluation.outcomes == ()


def test_every_candidate_is_kept():
    evaluation = evaluate(
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_both": "20/20",
            "cardiovascular.hypertension": True,
        },
    )
    assert {"vision", "cardiovascular"} <= {
        o["section"] for o in evaluation.all_outcomes()
    }


def test_prescribed_meds_clean_pass_is_sent_to_intake_end_to_end():
    evaluation = evaluate(
        N,
        {
            **VF_NORMAL,
            "visual_acuity.uncorrected_both": "20/20",
            "psychotropic_drugs.perscribed_drugs": True,
            "psychotropic_drugs.perscribed_drugs_compliant": True,
        },
    )
    assert evaluation.rule_engine_outcome_code == "CP"
    assert evaluation.selected_outcome_code == "IN"


# ---------------------------------------------------------------- failures


def test_invalid_rules_json_is_a_ruleset_error():
    with pytest.raises(RulesetError):
        Ruleset("{not json")
    with pytest.raises(RulesetError):
        Ruleset('{"a": 1}')


def test_input_rejected_by_the_ruleset_schema_is_an_evaluation_error():
    dmer = normalize(base_dmer())
    dmer["cns.mmse_score"] = "not a number"
    with pytest.raises(RuleEvaluationError):
        RULESET.evaluate(dmer, received_date=ago(days=5))


def test_checksum_identifies_the_ruleset():
    assert RULESET.checksum == Ruleset(RULES_JSON.encode("utf-8")).checksum
    assert len(RULESET.checksum) == 64


# ---------------------------------------------------------------- structure of rules.json

_GRAPH = json.loads(RULES_JSON)
_EXPRESSIONS = {
    e["key"]: e["value"]
    for n in _GRAPH["nodes"]
    if n["type"] == "expressionNode"
    for e in n["content"]["expressions"]
}
# Rows that read fields nothing produces yet -- attached reports are out of scope.
_OUT_OF_SCOPE_ROWS = {("Priority", 8), ("Priority", 9), ("Vision Impairment", 35)}
_OUT_OF_SCOPE_FIELDS = {
    "has_other_reports",
    "vision.has_report_attached",
    "vision.report_needs_r21_added",
    "vision.field_test_results_attached",
}
# DI fields the rules read that pass through normalization untouched, plus
# received_date, which the activity adds from dmer_document.
_PASS_THROUGH_FIELDS = {"type", "guide", "received_date"}
_FIELD_REF = re.compile(r"dmer\['([^']+)'\]|dmer\.(\w+)")


def _conditions():
    """(table, row number, condition text) for every decision-table row."""
    for name, content in _TABLES.items():
        condition_id = content["inputs"][0]["id"]
        for i, rule in enumerate(content["rules"], 1):
            yield name, i, rule.get(condition_id) or ""


def _all_rule_text():
    yield from (f"{t} R{i}: {text}" for t, i, text in _conditions())
    yield from (f"expr {k}: {v}" for k, v in _EXPRESSIONS.items())


def test_every_chapter_table_collects_candidates():
    not_collect = [
        name
        for name, content in _TABLES.items()
        if name != "DetermineLicenseCategory" and content.get("hitPolicy") != "collect"
    ]
    assert not_collect == []


def test_output_cells_are_valid_and_clean():
    problems = []
    for name, content in _TABLES.items():
        if name == "DetermineLicenseCategory":
            continue
        outputs = {o["field"]: o["id"] for o in content["outputs"]}
        for i, rule in enumerate(content["rules"], 1):
            action = json.loads(rule[outputs["action"]])
            reason_cell = rule[outputs["reason"]]
            reason = json.loads(reason_cell, strict=False)
            if action not in OUTCOME_PRIORITY:
                problems.append(f"{name} R{i}: action {action!r}")
            if rule[outputs["fit_letter"]] not in ("true", "false"):
                problems.append(
                    f"{name} R{i}: fit_letter {rule[outputs['fit_letter']]!r}"
                )
            # the ZEN expression parser doesn't decode \uXXXX escapes
            if (
                not reason
                or reason != reason.strip()
                or re.search(r"[\t\r\n]|\\u[0-9a-fA-F]{4}", reason_cell)
            ):
                problems.append(f"{name} R{i}: reason {reason_cell[:60]!r}")
    assert problems == []


def test_no_date_arithmetic_uses_minutes():
    # ZEN's "m" is minutes; months is "M"
    assert [
        t for t in _all_rule_text() if re.search(r'sub\(\s*\d+\s*,\s*"m"\s*\)', t)
    ] == []


def test_date_fields_are_never_compared_with_null():
    # normalized dates are "" when missing, never null
    assert [
        t for t in _all_rule_text() if re.search(r"_date'\]\s*(==|!=)\s*null", t)
    ] == []


def test_rules_only_read_fields_that_are_produced():
    known = (
        set(CONDITIONS)
        | set(META_FIELD_DEFAULTS)
        | _PASS_THROUGH_FIELDS
        | _OUT_OF_SCOPE_FIELDS
    )
    unknown = sorted(
        {a or b for text in _all_rule_text() for a, b in _FIELD_REF.findall(text)}
        - known
    )
    assert unknown == []


@pytest.mark.parametrize("licence", ["NORMAL", "HIGH"])
def test_no_row_errors_with_its_conditions_ticked_and_numbers_and_dates_blank(licence):
    """A comparison on a blank number throws inside ZEN, and a throwing row is silently skipped."""
    errors = []
    for table, row, text in _conditions():
        if (
            table == "DetermineLicenseCategory"
            or (table, row) in _OUT_OF_SCOPE_ROWS
            or not text
        ):
            continue
        dmer = normalize(base_dmer())
        for field in re.findall(r"dmer\['([^']+)'\]\s*==\s*true", text):
            dmer[field] = True
        context = {
            "dmer": {**dmer, "received_date": ago(days=5)},
            "license_category": licence,
        }
        context.update({key: False for key in _EXPRESSIONS})
        try:
            zen.evaluate_expression(text, context)
        except RuntimeError as exc:
            errors.append(f"{table} R{row}: {str(exc)[:60]}")
    assert errors == []


# ---------------------------------------------------------------- coverage sweeps

_NORMAL_VISION = {**VF_NORMAL, "visual_acuity.uncorrected_both": "20/20"}
_NOT_A_CONDITION = (
    "opinion.",
    "priority.",
    "recommendations.",
    "relationship_with_patient.",
    "visual_acuity.",
    "visual_field.",
    "vision.",
)


def test_every_diabetes_combination_gets_a_diabetes_outcome():
    """collect + the R8 fallback: no diabetes DMER goes without a diabetes outcome."""
    treatments = [
        (),
        ("diet",),
        ("oral_meds",),
        ("insulin",),
        ("insulin_secretagogues",),
        ("oral_meds", "insulin"),
        ("text only",),
    ]
    missing = []
    for (
        dmer_type,
        licence,
        treatment,
        non_compliant,
        stable,
        concerns,
        r22,
    ) in itertools.product(
        ["age", "other"],
        [N, H],
        treatments,
        [False, True],
        [True, False],
        [False, True],
        [False, True],
    ):
        if r22 and licence == N:
            continue
        fields = {
            **_NORMAL_VISION,
            "type": dmer_type,
            "endocrine.diabetes": True,
            "endocrine.diabetes.non_compliant": non_compliant,
            "endocrine.stable_bg_control": stable,
            "endocrine.diabetes_has_concerns": concerns,
            "restrictions": [22] if r22 else [],
            "endocrine.diabetes_treatment_mentioned_in_text": treatment
            == ("text only",),
        }
        fields.update(
            {f"endocrine.diabetes.{t}": True for t in treatment if t != "text only"}
        )
        if not any(o.section == "diabetes" for o in evaluate(licence, fields).outcomes):
            missing.append(fields)
    assert missing == []


@pytest.mark.parametrize("licence", [N, H])
def test_a_blank_dmer_fires_only_vision_rows(licence):
    """Nothing ticked, normal vision: no chapter or Priority row may fire (a row
    that fires for every DMER would mask missing coverage everywhere else)."""
    evaluation = evaluate(licence, _NORMAL_VISION)
    assert [o.reason for o in evaluation.outcomes if o.section != "vision"] == []
    assert evaluation.selected_outcome_code == "CP"


@pytest.mark.parametrize("licence", [N, H])
def test_no_non_vision_condition_falls_through_to_the_vision_result(licence):
    """Ticked on its own, every non-vision condition gets a chapter outcome or the
    Priority catch-all -- never just Vision's (usually CP) result."""
    silent = []
    for field, cfg in CONDITIONS.items():
        if (
            cfg["type"] != "bool"
            or field.startswith(_NOT_A_CONDITION)
            or field.endswith(("_has_concerns", "_has_concern"))
        ):
            continue
        evaluation = evaluate(licence, {**_NORMAL_VISION, field: True})
        if not any(o.section != "vision" for o in evaluation.outcomes):
            silent.append(field)
    assert silent == []


# ---------------------------------------------------------------- edge cases from the guide review

EDGE_ROWS = [  # (label, table, row, licence, fields, expected)
    # Diabetes under collect
    (
        "D1: NORMAL, AGE, oral meds -> R3 CP",
        "Diabetes",
        3,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.stable_bg_control": True,
        },
        True,
    ),
    (
        "D1: NORMAL, AGE, oral meds -> R5 IN does not fire",
        "Diabetes",
        5,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.stable_bg_control": True,
        },
        False,
    ),
    (
        "D5: HIGH, AGE, no treatment ticked -> R8 fallback",
        "Diabetes",
        8,
        H,
        {"endocrine.diabetes": True, "endocrine.stable_bg_control": True},
        True,
    ),
    (
        "D7: NORMAL, AGE, treatment only in text -> R8 fallback",
        "Diabetes",
        8,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.stable_bg_control": True,
            "endocrine.diabetes_treatment_mentioned_in_text": True,
        },
        True,
    ),
    (
        "both ticked, NORMAL, AGE, unstable -> R3 CP still fires",
        "Diabetes",
        3,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.diabetes.insulin": True,
        },
        True,
    ),
    (
        "both ticked, NORMAL, AGE, unstable -> R8 IN also fires",
        "Diabetes",
        8,
        N,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.diabetes.insulin": True,
        },
        True,
    ),
    (
        "both ticked, HIGH, AGE, R22 -> R7 IN fires alongside R4",
        "Diabetes",
        7,
        H,
        {
            "endocrine.diabetes": True,
            "endocrine.diabetes.oral_meds": True,
            "endocrine.diabetes.insulin": True,
            "restrictions": [22],
        },
        True,
    ),
    # Trails boundaries (guide: A under 90 s -> PU, 90 s or more -> CR; B 90 s to 3 min -> PU, over 3 min -> CR)
    (
        "Trails B 180 s -> R7 PU",
        "CognitiveImpairment",
        7,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_b_seconds": 180},
        True,
    ),
    (
        "Trails B 180 s -> R9 CR does not fire",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_b_seconds": 180},
        False,
    ),
    (
        "Trails B 181 s -> R9 CR",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_b_seconds": 181},
        True,
    ),
    (
        "Trails B 89 s -> R7 PU does not fire",
        "CognitiveImpairment",
        7,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_b_seconds": 89},
        False,
    ),
    (
        "Trails A 89 s -> R7 PU",
        "CognitiveImpairment",
        7,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_a_seconds": 89},
        True,
    ),
    (
        "Trails A 90 s -> R9 CR",
        "CognitiveImpairment",
        9,
        N,
        {"cns.cognitiveimpairment": True, "cns.trails_a_seconds": 90},
        True,
    ),
    # Vision
    (
        "bad eye exactly 20/100 counts as monocular -> R22 CP",
        "Vision Impairment",
        22,
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_left": "20/20",
            "visual_acuity.corrected_right": "20/100",
        },
        True,
    ),
    (
        "bad eye 20/80 is not monocular -> R22 does not fire",
        "Vision Impairment",
        22,
        N,
        {
            **VF_NORMAL,
            "visual_acuity.corrected_left": "20/20",
            "visual_acuity.corrected_right": "20/80",
        },
        False,
    ),
    # Section C 'Yes'
    (
        "Section C Yes + monocular (vision only) -> Priority R5 PU fallback",
        "Priority",
        5,
        N,
        {**_NORMAL_VISION, "opinion.yes": True, "vision.monocular": True},
        True,
    ),
    # Dates measured from the exam date, today when it is blank
    (
        "seizure 4 months ago, blank exam date -> R3 PU (measured from today)",
        "Seizures",
        3,
        N,
        {
            "cns.epilepsy": True,
            "cns.seizure_date": ago(months=4),
            "medical_examination_date": "",
        },
        True,
    ),
    # Catch-all only fires when no chapter did (known limitation)
    (
        "bipolar + hypertension -> catch-all R11 does not fire",
        "Priority",
        11,
        N,
        {"psychiatric.bipolar": True, "cardiovascular.hypertension": True},
        False,
    ),
    # Guide contradictions kept as written
    (
        "CV3: CVA, no date, concerns -> R11 PU",
        "Cerebrovascular",
        11,
        N,
        {"cerebrovascular.cva": True, "cerebrovascular.cva_tia_has_concern": True},
        True,
    ),
    (
        "CV3: CVA, no date, concerns -> R13 PR also fires",
        "Cerebrovascular",
        13,
        N,
        {"cerebrovascular.cva": True, "cerebrovascular.cva_tia_has_concern": True},
        True,
    ),
    (
        "M5: paraplegia with R25, no concerns -> R14 TCM",
        "Musculoskeletal",
        14,
        N,
        {"musculoskeletal.paraplegia": True, "restrictions": [25]},
        True,
    ),
    # other_psych_diagnosis is the catch-all for a written diagnosis that isn't a named one
    (
        "written 'schizoaffective disorder' -> R23 CP via other psych diagnosis",
        "Psychiatric Disorders",
        23,
        N,
        {
            "psychiatric.psych_diagnosis": "schizoaffective disorder",
            "psychiatric.stable_condition": True,
            "psychiatric.compliant_with_treatment": True,
        },
        True,
    ),
    (
        "written 'bipolar' (flagged as bipolar) -> R23 does not fire",
        "Psychiatric Disorders",
        23,
        N,
        {
            "psychiatric.psych_diagnosis": "bipolar",
            "psychiatric.bipolar": True,
            "psychiatric.stable_condition": True,
            "psychiatric.compliant_with_treatment": True,
        },
        False,
    ),
    # Restriction text formats
    (
        "'R20' makes a Class 5 licence HIGH (corrected 20/40 -> IN)",
        None,
        None,
        N,
        {
            "restrictions": "R20",
            "visual_field.normal": True,
            "visual_acuity.corrected_both": "20/40",
        },
        "IN",
    ),
]


@pytest.mark.parametrize(
    ("label", "table", "row", "licence", "fields", "expected"),
    EDGE_ROWS,
    ids=[r[0] for r in EDGE_ROWS],
)
def test_edge_case_row(label, table, row, licence, fields, expected):
    evaluation = evaluate(licence, fields)
    if table is None:
        assert vision_action(evaluation) == expected
    else:
        assert row_fired(evaluation, table, row) is expected


@pytest.mark.parametrize(
    ("label", "fields", "engine", "selected"),
    [
        (
            "D1: NORMAL, AGE, oral meds, normal vision",
            {
                "endocrine.diabetes": True,
                "endocrine.diabetes.oral_meds": True,
                "endocrine.stable_bg_control": True,
            },
            "CP",
            "CP",
        ),
        (
            "active SUD: PU, no driving-record override",
            {"psychotropic_drugs.substance_use": True},
            "PU",
            "PU",
        ),
        (
            "abuse/dependence, no concerns: CP sent to intake",
            {"psychotropic_drugs.substance_abuse_or_dependence": True},
            "CP",
            "IN",
        ),
        (
            "abuse/dependence with concerns: PR stays PR",
            {
                "psychotropic_drugs.substance_abuse_or_dependence": True,
                "psychotropic_drugs.substance_use_has_concerns": True,
            },
            "PR",
            "PR",
        ),
        (
            "Section C Yes + hypertension: CP, not CR",
            {"opinion.yes": True, "cardiovascular.hypertension": True},
            "CP",
            "CP",
        ),
        (
            "bipolar, no details: catch-all IN, not vision CP",
            {"psychiatric.bipolar": True},
            "IN",
            "IN",
        ),
        (
            "pulmonary embolism, unresolved: CP",
            {"respiratory.pulmonary_embolism": True},
            "CP",
            "CP",
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and len(v) > 3 else None,
)
def test_edge_case_selected_outcome(label, fields, engine, selected):
    evaluation = evaluate(N, {**_NORMAL_VISION, **fields})
    assert (evaluation.rule_engine_outcome_code, evaluation.selected_outcome_code) == (
        engine,
        selected,
    )
