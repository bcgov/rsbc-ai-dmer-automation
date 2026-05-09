"""
Prompt construction for DMER field validation.
===============================================
Builds the system prompt that embeds the full conditions list with type
hints and description metadata, so the LLM knows exactly which fields to
evaluate.
"""

from src.conditions import CONDITIONS


def build_conditions_list() -> str:
    """Return a formatted string listing every condition for the system prompt."""
    lines = []
    for name, cfg in CONDITIONS.items():
        desc = cfg.get("description", "")
        if desc:
            lines.append(f"  - {name} ({cfg['type']}) [description: {desc}]")
        else:
            lines.append(f"  - {name} ({cfg['type']})")
    return "\n".join(lines)


CONDITIONS_LIST = build_conditions_list()

SYSTEM_PROMPT = f"""Analyze the provided DMER (Driver's Medical Examination Report) fields.

You will receive a filtered subset of the DMER JSON containing only:
- Written/text fields with values (dates, scores, free-text details, medical narratives)
- Checkbox fields that are marked TRUE (indicating active/checked conditions)

All unchecked (false) checkboxes and empty text fields have been omitted.

CRITICAL — Free-text field analysis:
You MUST carefully read and extract conditions from ALL free-text fields, including "details_of_condition" and EVERY field ending in ".other" (e.g. "endocrine.other", "general.other") or "_details".
READ ENTIRE SENTENCES FULLY to understand context and causality. DO NOT isolate keywords. If a symptom is explicitly caused by a primary disease 
(e.g., "symptom X due to disease Y", or "patient has disease X and symptom Y"), it is ONLY a concern for that primary disease (setting its _has_concerns to TRUE). NEVER flag secondary symptoms as separate independent conditions.
Set the corresponding boolean field(s) to true for EVERY independent medical condition, diagnosis, or procedure mentioned.
For example:
- "BIL CATARACT EXTRACTIONS" → set vision.cataracts=true AND vision.cataracts_had_surgery=true
- "hx of TIA" → set cerebrovascular.tia=true
- "s/p CABG" → set cardiovascular.cad=true
- "OSA on CPAP" → set sleep.obstructive_sleep_apnea=true AND sleep.cpap=true
- "insulin-dependent DM" → set endocrine.diabetes=true AND endocrine.diabetes.insulin=true
A surgical procedure (extraction, resection, repair, transplant, implant) implies BOTH the
condition AND the corresponding surgery/procedure field should be set to true.

RULE — *_has_concerns / *_has_concern fields:
Set the _has_concerns companion to TRUE if the parent condition is true AND either:
 1. opinion.yes is true, OR
 2. Any text describes symptoms, impacts, complications, or QUALITATIVE context for the condition.
 3. If there is language saying "controlled/under control", "stable", "compliant" etc. or mentioning the absence of a symptom ("no symptom X") then that would not count as a concern.

CRITICAL EXCEPTION FOR MEASUREMENTS/DATES:
Do NOT set _has_concerns to TRUE if the ONLY additional text is a quantitative measurement, date, size, or score that directly maps to another specific DMER field (e.g. "size 6.9 cm" mapping to pvd.aneurysm_size, or "HbA1C 10" mapping to endocrine.HbA1C). Quantitative values alone DO NOT constitute a "concern" if there is no other descriptive narrative.

When in doubt about QUALITATIVE descriptive narrative, ALWAYS err on the side of TRUE. 
CRITICAL: If the text MERELY NAMES OR LISTS the condition (e.g., "Patient has vertigo" or "Diagnosis: strabismus") with NO extra descriptive details, leave _has_concerns FALSE. The mere mention/existence of a condition sets the parent condition to TRUE, but it is NOT a concern by itself. The condition field CAN and OFTEN WILL be true while its _has_concerns companion remains false.
If the parent condition is true but lacks ANY descriptive context/concern (other than its existence or mapped measurements), leave _has_concerns false.
AMBIGUITY: If concerns in text could plausibly apply to multiple true parent conditions in the same category, set _has_concerns to true for ALL of them.

Also focus on:
- Checked (true) fields: these confirm conditions the physician identified on the form
- Written values: dates, scores, and free-text details provide additional clinical context

Here is the complete list of DMER conditions/fields to evaluate:
{CONDITIONS_LIST}

Each field above shows its expected data type in parentheses (bool, str, int, float).
For each field, determine if the provided data supports setting it.
- bool: set to true ONLY if evidence supports the condition
- str: set to the normalized/corrected value from the input
- int (scores like mmse_score, moca_score, trails_a_seconds, etc.): extract as integers
- float (like pvd.aneurysm_size, HbA1C): extract as floats

Common medical abbreviations: BIL/B/L=bilateral, PT=patient, s/p=status post, W.=with,
Hx=history, c/o=complains of, R/O=rule out.

Vision acuity text may be misread with the slash being read as 1 e.g. 20/80 might be read as 20180. If this is the case correct it.

RULE — Visual Acuity thresholds:
Convert acuity to Snellen before comparing (higher denominator = worse).
  Decimal: denom = 20/value (0.25→20/80). LogMAR: denom = 20×10^value (0.6→20/80).
Follow each field's [description] for which eyes/values to use.
Evaluate EVERY threshold independently. Example: 20/80 → _or_worse for 20/80,20/60,20/40 = true;
_or_better for 20/50,20/30 = false. If only one eye has data, use it where L/R is needed.

RULE — Evidence:
EVERY SINGLE bool set to true MUST have its own separate corresponding "{{field_name}}_evidence" string quoting the exact justification. 
CRITICAL MINIMUM REQUIREMENT: If you set BOTH a parent condition to true AND its `_has_concerns` companion to true, you MUST generate TWO SEPARATE evidence fields (e.g., one for `vision.strabismus_evidence` AND one for `vision.strabismus_has_concerns_evidence`). Never omit the evidence for the `_has_concerns` field! Non-bools need no evidence.


IMPORTANT: Return ONLY fields changed. Do NOT echo unchanged values.
NEVER EVER invent new field names; use ONLY the exact field names from the list above (plus their _evidence fields).
EXCEPTION: return ALL visual_acuity thresholds if ANY acuity exists.
Return your result as a flat JSON object under a "dmer" key.
Double-check: re-read ALL free-text fields to confirm you have NOT missed any conditions.

Example response format:
{{
  "dmer": {{
    "cardiovascular.cad": true,
    "cardiovascular.cad_evidence": "details_of_condition: 'patient has coronary artery disease'",
    "cardiovascular.cad_has_concerns": true,
    "cardiovascular.cad_has_concerns_evidence": "details_of_condition: 'may sometimes become confused due to loss of blood flow'",
    "cardiovascular.arrhythmia": true,
    "cardiovascular.arrhythmia_evidence": "arrhythmia checkbox was true",
    "cardiovascular.arrhythmia_type": "atrial fibrillation"
  }}
}}

Use semantic understanding to recognize variations and medical synonyms.
The [description: ...] hint for each field may contain a list of keywords or
more detailed instructions on how to normalize that field."""
