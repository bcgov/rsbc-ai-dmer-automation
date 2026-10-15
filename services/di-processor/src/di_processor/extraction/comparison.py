"""Duplicate-comparison fields and hash (``dmer_extraction.comparison_*``).

The Decision Gateway treats two waiting DMERs for the same driver as duplicates
when their ``comparison_hash`` values match. The hash is computed here, at
extraction time, from the custom-model output only (``top_level.json``): the
model returns the same values for the same page, whereas the LLM's handwriting
reading can differ between runs, so it would make two copies of one form look
different.

The comparison set follows the business sign-off ("exam date, physician
details, and checkbox answers"):

- the two dates, normalized to ``YYYY-MM-DD`` when they parse;
- every checkbox (selection-mark) field, as ``true``/``false``.

Physician details are left out: the custom model's
``relationship_with_patient.name_and_address`` read is unreliable (stamp or
handwriting, low confidence), and one wrong character would change the hash.

Values only — confidences, positions and every other field are ignored.
``comparison_fields`` is stored next to the hash so the gateway can show which
field differed. The set is versioned (``COMPARISON_VERSION`` is inside the
hashed payload) so changing it can never make an old and a new form match.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .schemas import TopLevelExtraction

# Bump whenever the field set or a normalization rule changes.
COMPARISON_VERSION = 1

# Exam date is handwritten; issued date is printed by RoadSafetyBC on the notice
# (same for every copy of one request, different for a new request).
DATE_FIELDS: tuple[str, ...] = (
    "medical_examination_date",
    "medical_issued_date",
)

# Every selection-mark field the custom model (rsbc-ocr-dmer-v9) returns. Fixed
# rather than detected at run time, so a model revision cannot silently change
# what is compared -- and because a box the model does not locate on a page
# comes back as "" rather than "unselected", so the value alone cannot tell a
# checkbox from an empty text field.
CHECKBOX_FIELDS: tuple[str, ...] = (
    "cardiovascular.arrhythmia",
    "cardiovascular.cad",
    "cardiovascular.congestive_heart_failure",
    "cardiovascular.icd",
    "cardiovascular.icd_primary",
    "cardiovascular.icd_secondary",
    "cardiovascular.icd_therapy",
    "cardiovascular.pacemaker",
    "cardiovascular.syncope",
    "cerebrovascular.cva",
    "cerebrovascular.tia",
    "cns.cognitiveimpairment",
    "cns.dementia",
    "cns.epilepsy",
    "cns.intracranial_tumors",
    "cns.non_progressive_stable",
    "cns.progressive_deficit",
    "cns.provoked_seizure",
    "cns.significant_head_injury",
    "endocrine.diabetes",
    "endocrine.diabetes.compliant",
    "endocrine.diabetes.diet",
    "endocrine.diabetes.insulin",
    "endocrine.diabetes.insulin_secretagogues",
    "endocrine.diabetes.non_compliant",
    "endocrine.diabetes.oral_meds",
    "endocrine.hypoglycemic_unawareness",
    "endocrine.persistent_hypoglycemic_unawareness",
    "endocrine.severe_hypoglycemia",
    "endocrine.stable_bg_control",
    "general.general_debility",
    "hearing.hearing_aid",
    "hearing.hearing_loss",
    "hearing.no_hearing_aid",
    "musculoskeletal.amputation_above_elbow",
    "musculoskeletal.amputation_above_knee",
    "musculoskeletal.amputation_below_elbow",
    "musculoskeletal.amputation_below_knee",
    "musculoskeletal.amputation_left_sided",
    "musculoskeletal.amputation_lower_limb",
    "musculoskeletal.amputation_right_sided",
    "musculoskeletal.amputation_upper_limb",
    "musculoskeletal.limb_amputation",
    "musculoskeletal.range_of_motion_loss",
    "musculoskeletal.uses_prosthesis",
    "musculoskeletal.vehicle_modifications",
    "musculoskeletal.weakness",
    "musculoskeletal.without_prosthesis",
    "opinion.maybe",
    "opinion.no",
    "opinion.yes",
    "psychiatric.compliant_with_treatment",
    "psychiatric.impaired_judgement",
    "psychiatric.non_compliant_with_treatment",
    "psychiatric.non_medical_therapy",
    "psychiatric.on_medications",
    "psychiatric.other_psych_diagnosis",
    "psychiatric.psychosis",
    "psychiatric.stable_condition",
    "psychiatric.unstable_condition",
    "psychotropic_drugs.alcohol_use",
    "psychotropic_drugs.alcohol_withdrawal_seizure",
    "psychotropic_drugs.narcotics",
    "psychotropic_drugs.perscribed_drugs",
    "psychotropic_drugs.perscribed_drugs_compliant",
    "psychotropic_drugs.perscribed_drugs_non_compliant",
    "psychotropic_drugs.psychoactive_drugs",
    "psychotropic_drugs.substance_use",
    "pvd.aneurysm",
    "pvd.peripheral_vascular_disease",
    "recommendations.restrictions",
    "recommendations.road_test_to_assess",
    "recommendations.specialist_consult_enclosed_no",
    "recommendations.specialist_consult_enclosed_yes",
    "recommendations.specialist_consult_will_arrange_no",
    "recommendations.specialist_consult_will_arrange_yes",
    "relationship_with_patient.first_visit",
    "relationship_with_patient.locum",
    "relationship_with_patient.np",
    "relationship_with_patient.specialist",
    "relationship_with_patient.walk_in",
    "respiratory.o2_continuous",
    "respiratory.o2_intermittent",
    "respiratory.o2_when_driving",
    "sleep.ahi",
    "sleep.cpap",
    "sleep.cpap_compliant",
    "sleep.cpap_non_compliant",
    "sleep.narcolepsy",
    "sleep.narcolepsy_controlled",
    "sleep.narcolepsy_uncontrolled",
    "sleep.no_daytime_sleepiness",
    "sleep.obstructive_sleep_apnea",
    "sleep.osa_mild",
    "sleep.osa_mod",
    "sleep.osa_severe",
    "sleep.with_daytime_sleepiness",
    "vestibular.drop_attacks",
    "vestibular.recurrent_vertigo",
    "vestibular.vertigo_with_warnings",
    "vestibular.vertigo_without_warnings",
    "vision.acuity_loss",
    "vision.field_defect",
    "vision.monocular",
    "visual_field.abnormal",
    "visual_field.meet_criteria_for_licence_class_yes",
    "visual_field.normal",
)

_SELECTED = "selected"
_MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "jan",
            "feb",
            "mar",
            "apr",
            "may",
            "jun",
            "jul",
            "aug",
            "sep",
            "oct",
            "nov",
            "dec",
        ],
        1,
    )
}
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
# "July 3 2025", "Jul 3, 2025"
_MONTH_DAY_YEAR = re.compile(r"^([a-z]{3,9})\s*(\d{1,2})\s*(\d{4})$")
# "3 July 2025", "27MAY2025"
_DAY_MONTH_YEAR = re.compile(r"^(\d{1,2})\s*([a-z]{3,9})\s*(\d{4})$")


def canonical_date(raw: str) -> str | None:
    """``YYYY-MM-DD`` for a date with a written month; else normalized text.

    Only dates with a month name are parsed: an all-numeric date such as
    ``07/03/2025`` is ambiguous (day/month order), so it is kept as normalized
    text — still stable between two scans of the same form. Empty -> ``None``.
    """
    text = " ".join(_NON_ALNUM.sub(" ", raw.lower()).split())
    if not text:
        return None
    for pattern, order in ((_MONTH_DAY_YEAR, "mdy"), (_DAY_MONTH_YEAR, "dmy")):
        match = pattern.match(text)
        if not match:
            continue
        a, b, year = match.groups()
        month_name, day = (a, b) if order == "mdy" else (b, a)
        month = _MONTHS.get(month_name[:3])
        if month and 1 <= int(day) <= 31:
            return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    return _NON_ALNUM.sub("", text)


def comparison_fields(top: TopLevelExtraction) -> dict[str, Any]:
    """The canonicalized comparison subset of the custom-model output.

    Every key is always present (a missing checkbox is ``False``, a missing
    date ``None``), so two copies of the same form produce the same shape.
    """
    fields = top.fields
    out: dict[str, Any] = {"version": COMPARISON_VERSION}
    for key in DATE_FIELDS:
        field = fields.get(key)
        out[key] = canonical_date(field.value) if field else None
    for key in CHECKBOX_FIELDS:
        field = fields.get(key)
        out[key] = bool(field) and field.value.strip().lower() == _SELECTED
    return out


def comparison_hash(fields: dict[str, Any]) -> str:
    """sha256 (64 hex chars) of the comparison fields' canonical JSON.

    Sorted keys and fixed separators make the text — and so the hash — depend
    only on the values, and anyone can recompute it from the stored jsonb.
    """
    text = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
