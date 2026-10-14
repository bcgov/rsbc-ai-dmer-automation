"""The rule engine's types for the fields Normalize passes straight through.

The normalization schema (schema.py) lists only the fields the LLM works on.
The rules' input schema (services/rule-engine/rules/rules.json, the
inputNode) also reads fields di-processor writes that Normalize neither
analyses nor derives: checkboxes and a few text fields. DI writes every value
as a string, and a checkbox it didn't locate as "" -- which the rules' input
schema rejects for a boolean, failing the whole document in the Rule Engine.
:func:`~dmer_common.normalization.pipeline.adapt_combined_fields` converts
each of these to the type below.

tests/test_rules_input.py checks this module against rules.json, so a
ruleset change that adds, drops or retypes one of these fields fails CI.
"""

from __future__ import annotations

from typing import Final

# Field -> the type the rules expect: "bool" (JSON boolean) or "str".
PASSTHROUGH_FIELD_TYPES: Final[dict[str, str]] = {
    # Checkboxes.
    "cns.significant_head_injury": "bool",
    "endocrine.diabetes.compliant": "bool",
    "hearing.hearing_aid": "bool",
    "hearing.no_hearing_aid": "bool",
    "musculoskeletal.amputation_above_elbow": "bool",
    "musculoskeletal.amputation_below_elbow": "bool",
    "musculoskeletal.uses_prosthesis": "bool",
    "musculoskeletal.without_prosthesis": "bool",
    "psychotropic_drugs.narcotics": "bool",
    "psychotropic_drugs.psychoactive_drugs": "bool",
    "recommendations.restrictions": "bool",
    "recommendations.specialist_consult_enclosed_no": "bool",
    "recommendations.specialist_consult_enclosed_yes": "bool",
    "recommendations.specialist_consult_will_arrange_no": "bool",
    "recommendations.specialist_consult_will_arrange_yes": "bool",
    "relationship_with_patient.family_physician_years_checkbox": "bool",
    "respiratory.o2_continuous": "bool",
    "respiratory.o2_intermittent": "bool",
    "respiratory.o2_when_driving": "bool",
    "sleep.ahi": "bool",
    "vestibular.vertigo": "bool",
    "vision.acuity_loss": "bool",
    "vision.field_defect": "bool",
    # Text.
    "cardiovascular.arrhythmia_type": "str",
    "dl_number": "str",
    "dob": "str",
    "general.other": "str",
    "psychiatric.psych_diagnosis": "str",
    "relationship_with_patient.name_and_address": "str",
}

# Rules input fields outside the normalization schema that are not passed
# through: Normalize computes them itself after adapt_combined_fields (``guide``
# must stay as written until then -- the guide parsing reads the raw text), or
# the Rule Engine adds them (``received_date``). Date fields are not listed:
# every ``*date*`` field already goes through the pipeline's date resolution,
# which writes an ISO date or "".
DERIVED_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "cns.cognitiveimpairment_s7_15_suspected",
        "diabetes_guide_9_1",
        "endocrine.diabetes_treatment_not_indicated",
        "guide",
        "guide_with_no_matching_condition",
        "no_other_conditions",
        "non_vision_condition_indicated",
        "received_date",
        "type",
    }
)
