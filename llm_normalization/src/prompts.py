"""
Prompt construction for DMER field validation.
===============================================
Builds the system prompt that embeds the full conditions list with type
hints and description metadata, so the LLM knows exactly which fields to
evaluate.
"""

from src.conditions import (
    CATEGORY_CONDITIONS,
    CATEGORY_INSTRUCTIONS,
    ConditionCategory,
)

# ---------------------------------------------------------------------------
# Category field index — auto-generated from the conditions schema
# ---------------------------------------------------------------------------

# Suffixes that mark helper / modifier fields not useful as routing hints
_INDEX_SKIP_SUFFIXES: tuple[str, ...] = (
    "_has_concerns", "_has_concern", "_evidence",
    "_date", "_cause", "_type", "_site", "_size",
    "_details", "_class", "_years", "_score",
)
# Local names that are structural, not condition names
_INDEX_SKIP_LOCALS: frozenset[str] = frozenset({
    "other", "yes", "no", "maybe", "details", "score",
    "date", "type", "site", "size", "cause", "class",
})
# Description starters that indicate instructions, not keyword lists.
# These are excluded from the categorization index but kept in field
# descriptions for the second-stage analysis LLM.
_INSTRUCTION_STARTERS: tuple[str, ...] = (
    "check ", "if ", "set ", "only ", "look ", "use ", "apply",
    "for ", "when ", "map ", "normalize", "recognize", "treat ",
    "focus ", "separate ", "extract ", "note:", "important:",
    "critical:", "rule", "based on", "must", "apply rules",
    "do not", "do NOT",
    # Visual acuity threshold computation instructions
    "corrected only", "both-eyes", "better eye", "worse eye", "e.g.",
)


def _build_category_field_index() -> str:
    """Return a per-category list of condition terms derived from the schema.

    Combines readable field names (snake_case → spaces) with short
    comma-style descriptions so the categorization LLM can route free-text
    terms by schema structure rather than general medical knowledge alone.

    This function is called once at module load time; the result is embedded
    in CATEGORY_SYSTEM_PROMPT.  Adding or renaming a field in conditions.py
    automatically updates the index — no manual maintenance required.
    """
    lines: list[str] = []
    for category in ConditionCategory:
        conditions = CATEGORY_CONDITIONS.get(category, {})
        seen: set[str] = set()
        terms: list[str] = []

        for field_name, cfg in conditions.items():
            # --- field-name term ---
            if not any(field_name.endswith(s) for s in _INDEX_SKIP_SUFFIXES):
                local = field_name.rsplit(".", 1)[-1]
                if local not in _INDEX_SKIP_LOCALS and len(local) >= 3:
                    readable = local.replace("_", " ")
                    key = readable.lower()
                    if key not in seen:
                        seen.add(key)
                        terms.append(readable)

            # --- description keywords ---
            desc = cfg.get("description", "").strip()
            if desc and len(desc) <= 120:
                dl = desc.lower()
                if not any(dl.startswith(s) for s in _INSTRUCTION_STARTERS):
                    for kw in desc.split(","):
                        kw = kw.strip().rstrip(".")
                        key = kw.lower()
                        if kw and len(kw) >= 3 and key not in seen:
                            seen.add(key)
                            terms.append(kw)

        if terms:
            lines.append(f"  {category.value}: {', '.join(terms)}")

    return "\n".join(lines)


_CATEGORY_FIELD_INDEX = _build_category_field_index()

print("-------- category field index --------")
print(_CATEGORY_FIELD_INDEX)


def build_conditions_list(conditions: dict[str, dict]) -> str:
    """Return a formatted string listing every condition for the system prompt."""
    lines = []
    for name, cfg in conditions.items():
        desc = cfg.get("description", "")
        if desc:
            lines.append(f"  - {name} ({cfg['type']}) [description: {desc}]")
        else:
            lines.append(f"  - {name} ({cfg['type']})")
    return "\n".join(lines)


def build_response_example(conditions: dict[str, dict]) -> str:
    """Return a response-format example using fields from the active category."""
    bool_field = next(
        (name for name, cfg in conditions.items() if cfg["type"] == "bool"),
        None,
    )
    value_field = next(
        (name for name, cfg in conditions.items() if cfg["type"] != "bool"),
        None,
    )

    lines = ['{', '  "dmer": {']
    if bool_field:
        lines.extend([
            f'    "{bool_field}": true,',
            f'    "{bool_field}_evidence": "field_name: exact supporting text"',
        ])
        if value_field:
            lines[-1] += ","
    if value_field:
        sample_value = "normalized value"
        if conditions[value_field]["type"] == "int":
            sample_value = "123"
        elif conditions[value_field]["type"] == "float":
            sample_value = "1.23"
        if conditions[value_field]["type"] in {"int", "float"}:
            lines.append(f'    "{value_field}": {sample_value}')
        else:
            lines.append(f'    "{value_field}": "{sample_value}"')
    lines.extend(["  }", "}"])
    return "\n".join(lines)


CATEGORY_LIST = "\n".join(f"  - {category.value}" for category in ConditionCategory)

CATEGORY_SYSTEM_PROMPT = f"""Categorize the provided DMER (Driver's Medical Examination Report) fields.

You will receive a filtered subset of the DMER JSON containing only:
- Written/text fields with values (dates, scores, free-text details, medical narratives)
- Checkbox fields that are marked TRUE (indicating active/checked conditions)

All unchecked (false) checkboxes and empty text fields have been omitted.

Your task is ONLY to choose which condition categories need detailed analysis.
Read all field names and all free-text values, including details_of_condition, .other fields, and _details fields.

Available categories:
{CATEGORY_LIST}

Return JSON only in this exact shape:
{{
  "categories": ["category_name"],
  "evidence": {{
    "category_name": "short reason copied or summarized from the DMER input"
  }}
}}

Include a category if:
- A field in that category is true
- A text/value field in that category has a value
- Free text mentions a diagnosis, procedure, symptom, score, date, measurement, or concern belonging to that category

When unsure, include the category. Do not include categories that have no supporting evidence.

Use the field index below to route free-text terms to the correct category.
Each line lists the condition terms that belong to that category in this schema:
{_CATEGORY_FIELD_INDEX}"""


def build_analysis_prompt(category: ConditionCategory) -> str:
    """Build the second-stage prompt for a single condition category."""
    conditions = CATEGORY_CONDITIONS[category]
    conditions_list = build_conditions_list(conditions)
    response_example = build_response_example(conditions)
    category_instruction = CATEGORY_INSTRUCTIONS.get(category, "")
    extra = f"\nCategory-specific guidance:\n{category_instruction}\n" if category_instruction else ""
    visual_acuity_exception = (
        "EXCEPTION: return ALL visual_acuity thresholds if ANY acuity exists.\n"
        if category is ConditionCategory.VISUAL_ACUITY
        else ""
    )

    prompt = f"""Analyze the provided DMER (Driver's Medical Examination Report) fields for the {category.value} category only.

You will receive a filtered subset of the DMER JSON containing only:
- Written/text fields with values (dates, scores, free-text details, medical narratives)
- Checkbox fields that are marked TRUE (indicating active/checked conditions)

All unchecked (false) checkboxes and empty text fields have been omitted.

CRITICAL — Free-text field analysis:
You MUST carefully read and extract conditions from ALL free-text fields, including "details_of_condition" and EVERY field ending in ".other" (e.g. "endocrine.other", "general.other") or "_details".
READ ENTIRE SENTENCES FULLY to understand context and causality. DO NOT isolate keywords. If a symptom is explicitly caused by a primary disease
(e.g., "symptom X due to disease Y", or "patient has disease X and symptom Y"), it is ONLY a concern for that primary disease (setting its _has_concerns to TRUE). NEVER flag secondary symptoms as separate independent conditions.
Set the corresponding boolean field(s) to true for EVERY independent medical condition, diagnosis, or procedure mentioned.

RULE — *_has_concerns / *_has_concern fields:
Set the _has_concerns companion to TRUE if the parent condition is true AND either:
 1. opinion.yes is true, OR
 2. Any text describes symptoms, impacts, complications, or QUALITATIVE context for the condition.
 3. If there is language saying "controlled/under control", "stable", "compliant" etc. or mentioning the absence of a symptom ("no symptom X") then that would not count as a concern.

CRITICAL EXCEPTION FOR MEASUREMENTS/DATES:
Do NOT set _has_concerns to TRUE if the ONLY additional text is a quantitative measurement, date, size, or score that directly maps to another specific DMER field. Quantitative values alone DO NOT constitute a "concern" if there is no other descriptive narrative.

When in doubt about QUALITATIVE descriptive narrative, ALWAYS err on the side of TRUE.
CRITICAL: If the text MERELY NAMES OR LISTS the condition (e.g., "Patient has vertigo" or "Diagnosis: strabismus") with NO extra descriptive details, leave _has_concerns FALSE. The mere mention/existence of a condition sets the parent condition to TRUE, but it is NOT a concern by itself. The condition field CAN and OFTEN WILL be true while its _has_concerns companion remains false.
If the parent condition is true but lacks ANY descriptive context/concern (other than its existence or mapped measurements), leave _has_concerns false.
AMBIGUITY: If concerns in text could plausibly apply to multiple true parent conditions in the same category, set _has_concerns to true for ALL of them.

Also focus on:
- Checked (true) fields: these confirm conditions the physician identified on the form
- Written values: dates, scores, and free-text details provide additional clinical context

Here is the complete list of DMER conditions/fields to evaluate for this category:
{conditions_list}
{extra}

Each field above shows its expected data type in parentheses (bool, str, int, float).
For each field, determine if the provided data supports setting it.
- bool: set to true ONLY if evidence supports the condition
- str: set to the normalized/corrected value from the input
- int: extract as integers
- float: extract as floats

Common medical abbreviations: PT=patient, W.=with, Hx=history, c/o=complains of, R/O=rule out.

RULE — Evidence:
EVERY SINGLE bool set to true MUST have its own separate corresponding "{{field_name}}_evidence" string quoting the exact justification.
CRITICAL MINIMUM REQUIREMENT: If you set BOTH a parent condition to true AND its `_has_concerns` companion to true, you MUST generate TWO SEPARATE evidence fields (e.g., one for `vision.strabismus_evidence` AND one for `vision.strabismus_has_concerns_evidence`). Never omit the evidence for the `_has_concerns` field! Non-bools need no evidence.


IMPORTANT: Return ONLY fields changed. Do NOT echo unchanged values.
NEVER EVER invent new field names; use ONLY the exact field names from the list above (plus their _evidence fields).
{visual_acuity_exception}
Return your result as a flat JSON object under a "dmer" key.
Double-check: re-read ALL free-text fields to confirm you have NOT missed any conditions.

Example response format:
{response_example}

Use semantic understanding to recognize variations and medical synonyms.
The [description: ...] hint for each field may contain a list of keywords or
more detailed instructions on how to normalize that field."""


    # print("conditions list:")
    # print(conditions_list)
    # print("prompt:")
    # print(prompt)
    return prompt
