"""DMER normalization pipeline (Document Orchestration's Activity: Normalize
-- see docs/development/stages/04-activity-normalize.md).

Ported from the ``llm_normalization`` POC, with real changes made on port
(this is not a straight copy):

- Azure OpenAI calls go through the injected :class:`dmer_common.openai_client.OpenAIClient`
  (retry + circuit breaker already wired there) instead of a raw SDK client
  built by this module -- matching di-processor's dependency-injection
  convention (see services/di-processor/src/di_processor/pipeline.py).
- No raw DMER content (clinical text, LLM responses) is ever logged --
  the POC printed full JSON payloads to the console for local debugging;
  this module logs field counts/category names only, matching
  ``OpenAIClient.complete()``'s own "never log message content" convention.
- Added: :func:`adapt_combined_fields` and :func:`standardize_field_types`,
  which deterministically standardize di-processor's real
  ``CombinedExtraction.fields`` (flat, every value a string, DI field names)
  into the typed ``{"dmer": {...}}`` shape the rest of this pipeline expects
  -- the input is standardized, never rejected -- plus schema validation of
  each model call's output and per-value evidence checks (see
  :func:`normalize_document`).
- :func:`normalize_document` is new: the single entry point the Normalize
  activity calls, composing every step below in the right order.

Confirmed contracts this module relies on (see git history / PR discussion
for how these were verified, not assumed):
- Every value in ``CombinedExtraction.fields`` is a string; checkboxes
  arrive as ``"selected"``/``"unselected"`` (a real DI sample), blank, or
  ``"true"``/``"false"`` -- :func:`_to_bool` accepts all of them.
- MMSE/MoCA scores are written "x/30" on the source form (confirmed
  against docs/development/stages/07-decision-gateway.md's own example).
"""

from __future__ import annotations

import calendar
import json
import math
import os
import re
from copy import deepcopy
from datetime import UTC, date, datetime

from dateutil import parser as dateparser

from ..openai_client import OpenAIClient
from ..telemetry import get_logger
from .errors import (  # NormalizationValidationError is part of this module's API
    InvalidModelOutput,
    NormalizationValidationError,  # noqa: F401
    call_model,
)
from .evidence import check_evidence
from .prompts import (
    _INDEX_SKIP_LOCALS,
    _INDEX_SKIP_SUFFIXES,
    _INSTRUCTION_STARTERS,
    CATEGORY_SYSTEM_PROMPT,
    build_analysis_prompt,
)
from .schema import (
    ALWAYS_ANALYZE_CATEGORIES,
    CATEGORY_CONDITIONS,
    CATEGORY_PREFIXES,
    CONDITIONS,
    EXTRACT_FIELDS,
    GUIDE_SECTION_CATEGORIES,
    META_FIELD_DEFAULTS,
    TYPE_DEFAULTS,
    ConditionCategory,
)

_log = get_logger(__name__)

# Schema version recorded on the dmer_stage_run row (model_version =
# "<deployment>@<this>") -- versioned independently of the extraction
# structuring prompt, per 04-activity-normalize.md's own recommendation.
NORMALIZATION_SCHEMA_VERSION = "normalization-schema-v3"

# LLM temperature for each stage -- both default to 0.0 (deterministic
# structured extraction); overridable via env var for experimentation
# without a code change, matching this codebase's general config convention.
CATEGORY_TEMPERATURE = float(
    os.environ.get("NORMALIZATION_CATEGORY_TEMPERATURE", "0.0")
)
ANALYZE_TEMPERATURE = float(os.environ.get("NORMALIZATION_ANALYZE_TEMPERATURE", "0.0"))


# ---------------------------------------------------------------------------
# Input adapter: di-processor's CombinedExtraction.fields -> typed dmer dict
# ---------------------------------------------------------------------------


# DI custom-model field names that differ from this schema's. The DI field
# list is fixed (the model's fields.json), so this is a static rename table.
DI_FIELD_ALIASES: dict[str, str] = {
    "current_license_class": "current_licence_class",
}

# Written values that mean "nothing recorded".
_BLANK_STRINGS = frozenset(
    {"", "n/a", "na", "none", "null", "nil", "-", "--", "unknown", "not done"}
)
# Written values that mean "no" for a checkbox. Any other non-blank text
# counts as "yes" -- a stray value errs toward reviewing the condition.
_FALSE_STRINGS = frozenset({"false", "no", "n", "0", "unchecked", "unselected", "off"})
# The exact values DI writes for a checkbox (selection mark).
_CHECKBOX_STRINGS = {
    "selected": True,
    "unselected": False,
    ":selected:": True,
    ":unselected:": False,
    "true": True,
    "false": False,
}


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip().lower() in _BLANK_STRINGS
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _to_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return not _is_blank(value) and value.strip().lower() not in _FALSE_STRINGS
    return False


def adapt_combined_fields(fields: dict[str, object]) -> dict:
    """Turn di-processor's extraction output into the ``{"dmer": {...}}``
    shape the rest of this pipeline works on. Deterministic; never rejects
    input.

    ``CombinedExtraction.fields`` is flat and every value is a plain string,
    checkboxes included (normally "true"/"false"). Here:

    - DI field names that differ from the schema are renamed
      (:data:`DI_FIELD_ALIASES`).
    - Checkbox fields become real bools (see :func:`_to_bool`).
    - Blank number fields become None. Non-blank ones stay as written so the
      analysis model can still read e.g. "approx 35-40%";
      :func:`standardize_field_types` converts them after analysis.
    - Missing text fields become "".

    A field this schema doesn't recognize is passed through unchanged --
    permissive, matching ``TopLevelExtraction``'s own "kept permissive so a
    model revision that adds fields does not break parsing" design -- except
    that a checkbox value ("selected"/"unselected"/"true"/"false") still
    becomes a bool.
    """
    dmer: dict[str, object] = {}
    for key, value in fields.items():
        name = DI_FIELD_ALIASES.get(key, key)
        if name != key and _is_blank(value) and not _is_blank(dmer.get(name)):
            continue  # a blank alias never overwrites a real value
        cfg = CONDITIONS.get(name)
        if cfg is None:
            # Not a schema field, but a DI checkbox is still a checkbox.
            checkbox = (
                _CHECKBOX_STRINGS.get(value.strip().lower())
                if isinstance(value, str)
                else None
            )
            dmer[name] = value if checkbox is None else checkbox
        elif cfg["type"] == "bool":
            dmer[name] = _to_bool(value)
        elif cfg["type"] in ("int", "float"):
            dmer[name] = None if _is_blank(value) else value
        else:
            dmer[name] = "" if value is None else value
    return {"dmer": dmer}


# Written psychiatric diagnosis fields on the form, and the diagnoses the
# guide names explicitly. other_psych_diagnosis is the catch-all for the rest.
_OTHER_PSYCH_TEXT_FIELDS = ("psychiatric.other", "psychiatric.psych_diagnosis")
_DEFINED_PSYCH_DIAGNOSES = (
    "psychiatric.adhd",
    "psychiatric.add",
    "psychiatric.ocd",
    "psychiatric.anxiety",
    "psychiatric.ptsd",
    "psychiatric.mild_depression",
    "psychiatric.autism",
    "psychiatric.mental_handicap",
    "psychiatric.psychosis",
    "psychiatric.bipolar",
    "psychiatric.schizophrenia",
)


def derive_other_psych_diagnosis(dmer_result: dict) -> dict:
    """``psychiatric.other_psych_diagnosis`` is the catch-all psychiatric
    diagnosis. It is true when its checkbox is ticked or the analysis model
    set it (e.g. "severe depression" in Section D), and also when a written
    psychiatric diagnosis field is filled in but none of the diagnoses the
    guide names explicitly was found -- so a written "bipolar" stays bipolar,
    while anything unrecognised is still caught. Runs after analysis, since
    whether the written text matched a named diagnosis is only known then.
    Never clears a true value.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    field = "psychiatric.other_psych_diagnosis"
    if _to_bool(dmer.get(field)):
        dmer[field] = True
    else:
        source = next(
            (f for f in _OTHER_PSYCH_TEXT_FIELDS if not _is_blank(dmer.get(f))), None
        )
        named = any(_to_bool(dmer.get(f)) for f in _DEFINED_PSYCH_DIAGNOSES)
        dmer[field] = source is not None and not named
        if dmer[field]:
            dmer.setdefault(f"{field}_evidence", f"{source}: {dmer[source]}")
    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


def _is_concern_field(name: str) -> bool:
    """Return True if *name* is a has_concerns / has_concern flag."""
    return name.endswith(("_has_concerns", "_has_concern", ".has_concerns"))


def _find_parent_field(concern_field: str, known_fields: set[str]) -> str | None:
    """Derive the parent condition field for a concern flag.

    Returns the parent field name if found, or None for category-level
    concerns (e.g. ``respiratory.has_concerns``).
    """
    for suffix in ("_has_concerns", "_has_concern", ".has_concerns"):
        if not concern_field.endswith(suffix):
            continue
        candidate = concern_field[: -len(suffix)]
        if candidate in known_fields:
            return candidate
        # Handle underscore vs. no-underscore mismatch
        # e.g. cns.cognitive_impairment → cns.cognitiveimpairment
        parts = candidate.rsplit(".", 1)
        if len(parts) == 2:
            alt = parts[0] + "." + parts[1].replace("_", "")
            if alt in known_fields:
                return alt
        return None  # suffix matched but parent not found → category-level
    return None


def extract_llm_fields(dmer_json: dict) -> dict:
    """Extract only the fields needed for LLM analysis to reduce prompt tokens.

    Includes:
    1. Written/text fields from EXTRACT_FIELDS (dates, scores, free-text) that have values
    2. Any checkbox (boolean) field that is True — these indicate checked conditions

    Skips boolean fields that are False and text fields that are empty/missing.
    """
    dmer = dmer_json.get("dmer", dmer_json)
    slim: dict = {}

    # 1. Include written-text fields that have actual values
    for key in EXTRACT_FIELDS:
        if key in dmer:
            val = dmer[key]
            if _is_empty_value(val):
                continue
            slim[key] = val

    # 2. Include any boolean field that is True (checked checkboxes)
    for key, val in dmer.items():
        if val is True:
            slim[key] = val

    return {"dmer": slim}


def _parse_categories(raw_categories: list[object]) -> list[ConditionCategory]:
    """Convert LLM category strings into known enum values."""
    categories: list[ConditionCategory] = []
    seen: set[ConditionCategory] = set()

    for raw in raw_categories:
        if not isinstance(raw, str):
            continue
        try:
            category = ConditionCategory(raw.strip())
        except ValueError:
            _log.warning("ignoring unknown category from LLM")
            continue
        if category not in seen:
            categories.append(category)
            seen.add(category)

    for category in ALWAYS_ANALYZE_CATEGORIES:
        if category not in seen:
            categories.append(category)
            seen.add(category)

    return categories


def categorize_conditions(
    openai: OpenAIClient, slim_json: dict
) -> list[ConditionCategory]:
    """First LLM call: choose which condition categories need analysis."""
    json_str = json.dumps(slim_json, indent=2)

    def accept(result: dict) -> list[ConditionCategory]:
        raw_categories = result.get("categories")
        if not isinstance(raw_categories, list) or any(
            not isinstance(c, str) for c in raw_categories
        ):
            raise InvalidModelOutput("categories must be a list of strings")
        return _parse_categories(raw_categories)

    categories = call_model(
        openai,
        [
            {"role": "system", "content": CATEGORY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Categorize this DMER JSON:\n\n{json_str}"},
        ],
        temperature=CATEGORY_TEMPERATURE,
        accept=accept,
        step="categorize",
        source=[slim_json, [c.value for c in ConditionCategory]],
        known_keys=("categories",),
    )
    seen = set(categories)
    _force_categories_from_keywords(slim_json, categories, seen)
    _force_categories_from_schema_terms(slim_json, categories, seen)

    _log.info(
        "categories selected",
        extra={"categories": [c.value for c in categories]},
    )

    return categories


def filter_fields_for_category(
    slim_json: dict,
    category: ConditionCategory,
) -> dict:
    """Return the subset of slim_json relevant to one analysis category."""
    dmer = slim_json.get("dmer", slim_json)
    category_fields = set(CATEGORY_CONDITIONS[category])
    category_prefixes = CATEGORY_PREFIXES[category]
    cognition_prefixes = CATEGORY_PREFIXES[ConditionCategory.COGNITION]
    filtered: dict = {}

    for key, value in dmer.items():
        if key == "details_of_condition":
            filtered[key] = value
            continue

        # Concern rules can depend on the physician's overall driving opinion.
        if key.startswith("opinion."):
            filtered[key] = value
            continue

        if key in category_fields:
            filtered[key] = value
            continue

        # Preserve same-category raw/source fields even if they are not canonical
        # output fields yet, e.g. vestibular.vertigo -> recurrent_vertigo.
        if any(key == prefix or key.startswith(prefix) for prefix in category_prefixes):
            if category is ConditionCategory.CNS and any(
                key == prefix or key.startswith(prefix) for prefix in cognition_prefixes
            ):
                continue
            filtered[key] = value

    return {"dmer": filtered}


def analyze_condition_category(
    openai: OpenAIClient,
    slim_json: dict,
    category: ConditionCategory,
) -> dict:
    """Second LLM call: analyze one category with a narrowed condition list."""
    if not CATEGORY_CONDITIONS[category]:
        return {"dmer": {}}

    category_json = filter_fields_for_category(slim_json, category)
    json_str = json.dumps(category_json, indent=2)
    return call_model(
        openai,
        [
            {"role": "system", "content": build_analysis_prompt(category)},
            {"role": "user", "content": f"Analyze this DMER JSON:\n\n{json_str}"},
        ],
        temperature=ANALYZE_TEMPERATURE,
        accept=lambda result: accept_analysis_output(category, result),
        step=f"analyze:{category.value}",
        source=category_json,
        known_keys=_ANALYSIS_KEYS,
    )


# Field names an analysis response may use (masked ones are restored to these).
_ANALYSIS_KEYS = frozenset(
    {"dmer", *CONDITIONS, *(f"{field}_evidence" for field in CONDITIONS)}
)


def accept_analysis_output(category: ConditionCategory, result: dict) -> dict:
    """Schema validation of one category's analysis output (docs step 3).

    Values are standardized first, with the same conversion DI input gets
    ("35%" -> 35, "yes" -> true); only what that can't fix is invalid and
    retried: a structure that isn't an object, or a known field whose value
    has nothing convertible in it. Field names are resolved, not retried: a
    concern companion spelled differently from the schema's maps to the real
    one (:func:`_resolve_field_name`); any other unknown field, or a real
    field from another category, is dropped. (The model repeats an invented
    name on every attempt, so retrying for it only turns the document into
    poison.)
    """
    fields = result.get("dmer", result)
    if not isinstance(fields, dict):
        raise InvalidModelOutput("analysis fields must be an object")
    allowed_fields = set(CATEGORY_CONDITIONS[category])
    accepted: dict = {}
    for raw_name, value in fields.items():
        is_evidence = raw_name.endswith("_evidence")
        field = _resolve_field_name(raw_name.removesuffix("_evidence"))
        if field is None or field not in allowed_fields:
            _log.warning(
                "ignoring field not in this category's schema",
                extra={"category": category.value},
            )
            continue
        field_name = f"{field}_evidence" if is_evidence else field
        if is_evidence:
            if value is not None:
                accepted[field_name] = (
                    value if isinstance(value, str) else json.dumps(value)
                )
            continue
        accepted[field_name] = _standardize_model_value(field, value)
    return {"dmer": accepted}


_CONCERN_SUFFIXES = ("_has_concerns", "_has_concern", ".has_concerns")


def _resolve_field_name(name: str) -> str | None:
    """The schema field *name* refers to, or None. A concern companion may be
    spelled with any of the schema's concern suffixes (e.g. the model's
    ``traumatic_brain_injury_has_concerns`` is the schema's
    ``traumatic_brain_injury.has_concerns``)."""
    if name in CONDITIONS:
        return name
    for suffix in _CONCERN_SUFFIXES:
        if name.endswith(suffix):
            base = name[: -len(suffix)]
            for candidate in (base + other for other in _CONCERN_SUFFIXES):
                if candidate in CONDITIONS:
                    return candidate
    return None


def _standardize_model_value(field: str, value: object) -> object:
    field_type = CONDITIONS[field]["type"]
    if isinstance(value, dict) or (isinstance(value, list) and field != "restrictions"):
        raise InvalidModelOutput(
            f"{field}: expected {field_type}, got {type(value).__name__}"
        )
    if field_type == "bool":
        return _to_bool(value)
    if field_type in ("int", "float"):
        if _is_blank(value):
            return None
        number = (
            None if isinstance(value, bool) else _parse_number(field, value, field_type)
        )
        if number is None:
            raise InvalidModelOutput(f"{field}: no {field_type} in the value")
        return number
    if field == "restrictions":
        return value
    return _to_text(value)


def _has_non_priority_condition_match(updates: dict) -> bool:
    """Return True when a non-priority boolean field was matched."""
    for field_name, value in updates.get("dmer", {}).items():
        if field_name.startswith("priority.") or field_name.endswith("_evidence"):
            continue
        if value is True:
            return True
    return False


def _has_priority_details_signal(slim_json: dict) -> bool:
    """Return True when details_of_condition explicitly mentions priority language."""
    dmer = slim_json.get("dmer", slim_json)
    details = str(dmer.get("details_of_condition", "")).lower()
    if not details.strip():
        return False

    explicit_phrases = (
        "should not drive",
        "shouldn't drive",
        "can not drive",
        "cannot drive",
        "can't drive",
        "do not drive",
        "not drive",
        "stop driving",
        "unsafe to drive",
        "unfit to drive",
        "unfit for current class",
        "not fit for current class",
        "ability to drive",
        "fitness to drive",
        "fit for downgrade",
    )
    if any(phrase in details for phrase in explicit_phrases):
        return True

    if ("concern" in details or "unsafe" in details or "unfit" in details) and (
        "drive" in details or "driving" in details
    ):
        return True

    for cfg in CATEGORY_CONDITIONS[ConditionCategory.PRIORITY].values():
        description = cfg.get("description", "")
        for term in description.split(","):
            term = term.strip().lower()
            if len(term) >= 4 and term in details:
                return True

    return False


# ---------------------------------------------------------------------------
# Deterministic conflict-resolution
# ---------------------------------------------------------------------------


_COMPLIANCE_PAIRS = (
    (
        "psychiatric.compliant_with_treatment",
        "psychotropic_drugs.perscribed_drugs_compliant",
    ),
    (
        "psychiatric.non_compliant_with_treatment",
        "psychotropic_drugs.perscribed_drugs_non_compliant",
    ),
)


def resolve_conflicts(dmer_result: dict) -> dict:
    """Apply deterministic field-conflict rules that must not be left to the LLM.

    Called after ``apply_updates`` so every input and LLM-derived field is
    visible in the same dict.

    Rules
    -----
    - ``psychotropic_drugs.alcohol_withdrawal_seizure = True`` → ``cns.epilepsy``
      and ``cns.provoked_seizure`` are forced to ``True``. An alcohol
      withdrawal seizure is still a seizure -- it must not be recorded only
      under its cause and left invisible to every seizure-related field
      that matters for a driving-fitness decision.
    - ``psychiatric.compliant_with_treatment`` / ``non_compliant_with_treatment``
      → the matching ``psychotropic_drugs.perscribed_drugs_(non_)compliant``.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    # Loss of consciousness in the context of carotid stenosis maps to
    # pvd.carotid_stenosis_loss_consciousness, not to cardiovascular.syncope/loc.
    # Enforce in both directions: set the pvd field if any LOC signal exists
    # (from either pvd or cardiovascular), then clear the cardiovascular fields.
    if dmer.get("pvd.carotid_stenosis") is True:
        loc_signaled = (
            dmer.get("pvd.carotid_stenosis_loss_consciousness") is True
            or dmer.get("cardiovascular.syncope") is True
            or dmer.get("cardiovascular.loc") is True
        )
        if loc_signaled:
            dmer["pvd.carotid_stenosis_loss_consciousness"] = True
            for field in ("cardiovascular.syncope", "cardiovascular.loc"):
                if dmer.get(field) is True:
                    dmer[field] = False
                    dmer[f"{field}_evidence"] = (
                        "pvd.carotid_stenosis: true — loss of consciousness "
                        "attributed to carotid stenosis maps to "
                        "pvd.carotid_stenosis_loss_consciousness"
                    )

    if dmer.get("psychotropic_drugs.alcohol_withdrawal_seizure") is True:
        if dmer.get("cns.epilepsy") is not True:
            dmer["cns.epilepsy"] = True
            dmer["cns.epilepsy_evidence"] = (
                "psychotropic_drugs.alcohol_withdrawal_seizure: true — an "
                "alcohol withdrawal seizure is still a seizure"
            )
        if dmer.get("cns.provoked_seizure") is not True:
            dmer["cns.provoked_seizure"] = True
            dmer["cns.provoked_seizure_evidence"] = (
                "psychotropic_drugs.alcohol_withdrawal_seizure: true — "
                "alcohol withdrawal is the provoking cause, so this seizure "
                "is provoked rather than unprovoked/idiopathic epilepsy"
            )

    # Treatment compliance is ticked in the psychiatric section as often as in
    # the psychotropic-drugs one; the rules read the psychotropic fields, so a
    # tick in either place counts for both.
    for psych, drugs in _COMPLIANCE_PAIRS:
        if dmer.get(psych) is True and dmer.get(drugs) is not True:
            dmer[drugs] = True
            dmer[f"{drugs}_evidence"] = f"{psych}: true"

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


# ---------------------------------------------------------------------------
# Deterministic field-format normalization
# ---------------------------------------------------------------------------
# These fields have a known, narrow set of raw shapes (a roman numeral, a
# "x/30" fraction, a bare value that may or may not already be a list) that
# don't need LLM judgment to convert -- and per the driving-fitness
# consequences riding on them, must not be left to the LLM's best effort.
# Each one returns None/[] (never guesses, never raises) when the raw value
# doesn't match a known shape, so a genuinely unparseable value is left for
# manual review rather than silently coerced into something wrong.
# ---------------------------------------------------------------------------
_NYHA_ROMAN_TO_INT: dict[str, int] = {"I": 1, "II": 2, "III": 3, "IV": 4}


def parse_nyha_class(raw: object) -> int | None:
    """Convert a NYHA functional class value to a plain int 1-4.

    Accepts a roman numeral ("III"), a digit already ("3" or 3), or an int.
    NYHA class is I-IV only -- any other value (including an out-of-range
    int) returns None rather than being guessed at.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):  # bool is an int subclass; exclude explicitly
        return None
    if isinstance(raw, int):
        return raw if raw in (1, 2, 3, 4) else None
    text = str(raw).strip().upper()
    if text in _NYHA_ROMAN_TO_INT:
        return _NYHA_ROMAN_TO_INT[text]
    if text.isdigit():
        value = int(text)
        return value if value in (1, 2, 3, 4) else None
    return None


_COGNITIVE_SCORE_OUT_OF_30_RE = re.compile(r"(\d{1,2})\s*/\s*30")


def parse_cognitive_score(raw: object) -> int | None:
    """Extract the numerator from an MMSE/MoCA score written as "x/30"
    (e.g. "26/30" -> 26; confirmed format for both scores on this form).

    A missing score must stay None (null), not 0 -- 0 is a real, very
    different score from "not recorded".
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw)
    text = str(raw).strip()
    match = _COGNITIVE_SCORE_OUT_OF_30_RE.search(text)
    if match:
        return int(match.group(1))
    if text.isdigit():  # already a bare number, e.g. DI extracted just "26"
        return int(text)
    return None


_RESTRICTION_CODE_RE = re.compile(r"\d+")


def normalize_restrictions(raw: object) -> list[int]:
    """Coerce the restrictions field to a list of integer restriction codes,
    whatever shape the source JSON gave it in (a scalar, a list, or a
    comma/semicolon-separated string). Integers, not strings: the rule
    engine tests membership with integer literals (``20 in dmer.restrictions``)
    and its input schema requires integer items. Parts with no digits are
    dropped; a part like "R20" yields 20.
    """
    if raw is None or isinstance(raw, bool):
        return []
    parts = raw if isinstance(raw, list) else re.split(r"[,;]", str(raw))
    codes: list[int] = []
    for part in parts:
        if isinstance(part, bool) or part is None:
            continue
        if isinstance(part, (int, float)):
            codes.append(int(part))
            continue
        match = _RESTRICTION_CODE_RE.search(str(part))
        if match:
            codes.append(int(match.group(0)))
    return codes


def apply_deterministic_field_formats(dmer_result: dict) -> dict:
    """Restriction codes plus :func:`standardize_field_types`, over the merged
    output. Called after apply_updates so it sees whatever the LLM itself may
    have already produced for these fields -- this pass guarantees the
    canonical form regardless, the same way normalize_dates guarantees dates
    regardless of what the LLM attempted on its own.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    if "restrictions" in dmer:
        dmer["restrictions"] = normalize_restrictions(dmer["restrictions"])
    return standardize_field_types(dmer_result)


# ---------------------------------------------------------------------------
# Type standardization: every CONDITIONS field ends up its declared type.
# DI sends every value as text, and the analysis model can return a value in
# any shape, so nothing here rejects a value -- an unreadable number becomes
# None ("not recorded") and is logged by field name only.
# ---------------------------------------------------------------------------
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
_MINUTES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:m\b|min)", re.IGNORECASE)
_SECONDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:s\b|sec)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"(\d+)\s*:\s*(\d{1,2})")
_NYHA_TOKEN_RE = re.compile(r"\b(IV|III|II|I|[1-4])\b")


def _first_number(text: str) -> float | None:
    match = _NUMBER_RE.search(text)
    return float(match.group(0).replace(",", ".")) if match else None


def parse_duration_seconds(raw: object) -> int | None:
    """A Trails A/B time in whole seconds: "190", "190 s", "3:10",
    "3 min 10 sec", "3 minutes" (-> 180)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return round(raw)
    text = str(raw)
    clock = _CLOCK_RE.search(text)
    if clock:
        return int(clock.group(1)) * 60 + int(clock.group(2))
    minutes = _MINUTES_RE.search(text)
    seconds = _SECONDS_RE.search(text)
    if minutes or seconds:
        total = float(minutes.group(1)) * 60 if minutes else 0.0
        total += float(seconds.group(1)) if seconds else 0.0
        return round(total)
    number = _first_number(text)
    return None if number is None else round(number)


def parse_aneurysm_size_cm(raw: object) -> float | None:
    """An aneurysm diameter in cm; a value written in mm is converted."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    number = _first_number(str(raw))
    if number is None:
        return None
    return number / 10 if "mm" in str(raw).lower() else number


# A roman numeral written by hand is often read as l / | for I ("lll", "Il").
_NYHA_OCR_NUMERAL_RE = re.compile(r"(?<![A-Za-z])[IVl|]{1,3}(?![A-Za-z])")


def _parse_nyha(raw: object) -> int | None:
    value = parse_nyha_class(raw)
    if value is None and isinstance(raw, str):
        text = _NYHA_OCR_NUMERAL_RE.sub(
            lambda m: m.group().replace("l", "I").replace("|", "I"), raw
        )
        token = _NYHA_TOKEN_RE.search(text.upper())
        value = parse_nyha_class(token.group(1)) if token else None
    return value


# Fields whose numbers need more than "the first number in the text".
_NUMBER_PARSERS = {
    "cardiovascular.nyha_class": _parse_nyha,
    "cns.mmse_score": parse_cognitive_score,
    "cns.moca_score": parse_cognitive_score,
    "cns.trails_a_seconds": parse_duration_seconds,
    "cns.trails_b_seconds": parse_duration_seconds,
    "pvd.aneurysm_size": parse_aneurysm_size_cm,
}


# NYHA is I-IV only: an out-of-range value must not fall back to "the first
# number in the text".
_NO_NUMBER_FALLBACK = frozenset({"cardiovascular.nyha_class"})


def _parse_number(field: str, value: object, field_type: str) -> int | float | None:
    """The number in *value* as *field_type*, or None if there is none."""
    parser = _NUMBER_PARSERS.get(field)
    number = parser(value) if parser else None
    if number is None and field not in _NO_NUMBER_FALLBACK:
        if isinstance(value, (int, float)):
            number = value
        elif isinstance(value, str):
            number = _first_number(value)
    if number is None or not math.isfinite(number):
        return None
    return round(number) if field_type == "int" else float(number)


def _to_number(field: str, value: object, field_type: str) -> int | float | None:
    if _is_blank(value) or isinstance(value, bool):
        return None
    number = _parse_number(field, value, field_type)
    if number is None:
        _log.warning("unreadable number left blank", extra={"field": field})
    return number


def _to_text(value: object) -> str:
    if value is None or value is False:
        return ""
    if isinstance(value, list):
        return "; ".join(str(v) for v in value if v is not None)
    return value if isinstance(value, str) else str(value)


def standardize_field_types(dmer_result: dict) -> dict:
    """Convert every CONDITIONS field present to its declared type: bool,
    int/float (or None), str. ``restrictions`` is left to
    normalize_restrictions (its final shape is a list of codes)."""
    dmer = dmer_result.get("dmer", dmer_result)
    for field, cfg in CONDITIONS.items():
        if field not in dmer or field == "restrictions":
            continue
        value, field_type = dmer[field], cfg["type"]
        if field_type == "bool":
            dmer[field] = _to_bool(value)
        elif field_type in ("int", "float"):
            dmer[field] = _to_number(field, value, field_type)
        else:
            dmer[field] = _to_text(value)
    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


_ACUITY_FRACTION_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def parse_visual_acuity_denominator(raw: object) -> int | None:
    """Extract the Snellen denominator from a raw visual acuity value
    (e.g. "20/50", "20/50cf", "20/50^cf^", "20 / 50"), ignoring any
    trailing superscript/footnote annotation (counting-fingers, hand
    motion, etc.) appended directly after the fraction -- the digit run
    for the denominator stops at the first non-digit character, so
    "50cf" and "50^cf^" both yield 50. Returns None when no numerator/
    denominator fraction is present.

    Known limitation, not handled: a *purely numeric* suffix with no
    separator (e.g. a hypothetical "20/502" meaning "20/50" plus a "2"
    superscript) is inherently ambiguous and is read as denominator 502 --
    every real example seen in the BC Guide uses a letter code (cf/hm/lp),
    not a bare digit, for this annotation.
    """
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    match = _ACUITY_FRACTION_RE.search(str(raw))
    return int(match.group(2)) if match else None


# ---------------------------------------------------------------------------
# visual_acuity threshold fields -- deterministic, not the LLM's judgment
# call. Recomputed from the raw corrected_*/uncorrected_* strings after
# every LLM pass, overwriting whatever the model itself attempted for
# these fields (same "Python is authoritative" precedent as
# apply_deterministic_field_formats, kept separate from it because this is
# a different kind of operation -- deriving new composite fields from
# other raw fields, with its own evidence bookkeeping -- not reformatting
# a field into its own canonical form).
#
#   corrected_only  -- True: only ever look at corrected_*; a field whose
#                      name says "corrected" must not silently fall back
#                      to an uncorrected measurement.
#                      False: corrected_* first, falling back to
#                      uncorrected_* only when NO corrected value exists
#                      at all (not "both", not "left", not "right").
#   worse_eye       -- False: the "_both" (binocular) value if present,
#                      else the better (lower-denominator) of left/right.
#                      True (bad_eye_20/100_or_worse only): the worse of
#                      left/right; "_both" is never used, because a
#                      binocular reading reflects the better eye and would
#                      hide the bad one.
#   op / denom      -- the threshold comparison: "worse_or_equal" (>=),
#                      "better_or_equal" (<=), or "strictly_worse" (>).
# ---------------------------------------------------------------------------
_ACUITY_THRESHOLD_FIELDS: dict[str, dict] = {
    "visual_acuity.corrected_vision_20/80_or_worse": {
        "corrected_only": True,
        "worse_eye": False,
        "op": "worse_or_equal",
        "denom": 80,
    },
    "visual_acuity.corrected_vision_20/60_or_worse": {
        "corrected_only": True,
        "worse_eye": False,
        "op": "worse_or_equal",
        "denom": 60,
    },
    "visual_acuity.corrected_vision_20/50_or_better": {
        "corrected_only": True,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 50,
    },
    "visual_acuity.corrected_vision_20/30_or_better": {
        "corrected_only": True,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 30,
    },
    "visual_acuity.corrected_vision_20/20_or_better": {
        "corrected_only": True,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 20,
    },
    "visual_acuity.vision_20/60_or_worse": {
        "corrected_only": False,
        "worse_eye": False,
        "op": "worse_or_equal",
        "denom": 60,
    },
    "visual_acuity.vision_20/40_or_worse": {
        "corrected_only": False,
        "worse_eye": False,
        "op": "worse_or_equal",
        "denom": 40,
    },
    "visual_acuity.vision_20/50_or_better": {
        "corrected_only": False,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 50,
    },
    "visual_acuity.vision_20/30_or_better": {
        "corrected_only": False,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 30,
    },
    "visual_acuity.vision_20/20_or_better": {
        "corrected_only": False,
        "worse_eye": False,
        "op": "better_or_equal",
        "denom": 20,
    },
    "visual_acuity.bad_eye_20/100_or_worse": {
        "corrected_only": False,
        "worse_eye": True,
        "op": "worse_or_equal",
        "denom": 100,
    },
}


def _resolve_eye_group_denominator(
    dmer: dict, prefix: str, *, worse_eye: bool
) -> tuple[int | None, str | None]:
    """Resolve one correction state's ("corrected" or "uncorrected")
    denominator: the "_both" (binocular) value if present, else the better
    of "_left"/"_right" -- or, when worse_eye is True, always the worse of
    "_left"/"_right", ignoring "_both".
    Returns (denominator, source_field_name) so callers can cite exactly
    which raw field the value came from as evidence.
    """
    if not worse_eye:
        both_field = f"visual_acuity.{prefix}_both"
        both = parse_visual_acuity_denominator(dmer.get(both_field))
        if both is not None:
            return both, both_field

    left_field = f"visual_acuity.{prefix}_left"
    right_field = f"visual_acuity.{prefix}_right"
    candidates = [
        (value, field_name)
        for value, field_name in (
            (parse_visual_acuity_denominator(dmer.get(left_field)), left_field),
            (parse_visual_acuity_denominator(dmer.get(right_field)), right_field),
        )
        if value is not None
    ]
    if not candidates:
        return None, None
    chosen = (
        max(candidates, key=lambda item: item[0])
        if worse_eye
        else min(candidates, key=lambda item: item[0])
    )
    return chosen


def _resolve_acuity_denominator(
    dmer: dict, *, corrected_only: bool, worse_eye: bool
) -> tuple[int | None, str | None]:
    """Resolve the denominator to compare against a threshold: corrected
    first, falling back to uncorrected only when corrected_only is False
    and no corrected value is present at all (see _ACUITY_THRESHOLD_FIELDS'
    per-field corrected_only setting).
    """
    denom, source = _resolve_eye_group_denominator(
        dmer, "corrected", worse_eye=worse_eye
    )
    if denom is not None or corrected_only:
        return denom, source
    return _resolve_eye_group_denominator(dmer, "uncorrected", worse_eye=worse_eye)


def _meets_acuity_threshold(cfg: dict, denom: int) -> bool:
    threshold = cfg["denom"]
    if cfg["op"] == "worse_or_equal":
        return denom >= threshold
    if cfg["op"] == "better_or_equal":
        return denom <= threshold
    return denom > threshold  # strictly_worse


_BAD_EYE_FIELD = "visual_acuity.bad_eye_20/100_or_worse"


def flag_monocular_from_bad_eye(dmer_result: dict) -> dict:
    """Set ``vision.monocular`` true when the bad eye is 20/100 or worse.

    The BC Guide treats that acuity as monocularity, like the checkbox.
    Called on the input before the LLM pass, so the analysis prompt sees
    the condition and can judge ``vision.monocular_has_concerns`` from
    Section D, and again after the final threshold pass, which uses the
    LLM's OCR-corrected acuity strings. Never clears an existing true.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    cfg = _ACUITY_THRESHOLD_FIELDS[_BAD_EYE_FIELD]
    denom, source_field = _resolve_acuity_denominator(
        dmer, corrected_only=cfg["corrected_only"], worse_eye=cfg["worse_eye"]
    )
    if (
        denom is not None
        and _meets_acuity_threshold(cfg, denom)
        and dmer.get("vision.monocular") is not True
    ):
        dmer["vision.monocular"] = True
        dmer["vision.monocular_evidence"] = (
            f"{source_field}=20/{denom} (bad eye 20/100 or worse)"
        )
    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


def apply_visual_acuity_thresholds(dmer_result: dict) -> dict:
    """Deterministically computes every field in _ACUITY_THRESHOLD_FIELDS
    from the raw corrected/uncorrected acuity strings, overwriting
    whatever the LLM itself may have set for them -- these are pure
    numeric comparisons, not judgment calls, so this pass is authoritative
    regardless of what the model attempted on its own (catches any error
    in the model's own arithmetic the same way apply_deterministic_field_formats
    does for NYHA/MMSE/MoCA/restrictions).

    Sets {field}_evidence (citing the exact source field and resolved
    denominator) whenever a field is set True: every true boolean CONDITIONS
    field without evidence is reported in evidence_flags (missing_evidence).
    A field left False needs no evidence.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    for field_name, cfg in _ACUITY_THRESHOLD_FIELDS.items():
        denom, source_field = _resolve_acuity_denominator(
            dmer, corrected_only=cfg["corrected_only"], worse_eye=cfg["worse_eye"]
        )
        if denom is None:
            dmer[field_name] = False
            continue

        is_true = _meets_acuity_threshold(cfg, denom)
        dmer[field_name] = is_true
        if is_true:
            dmer[f"{field_name}_evidence"] = f"{source_field}=20/{denom}"

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


# ---------------------------------------------------------------------------
# Deterministic keyword-based category forcing
# ---------------------------------------------------------------------------
# Each rule is a tuple of:
#   (match_re, exclude_re | None, forced_category, log_label)
#
# The rule fires when:
#   - match_re matches details_of_condition, AND
#   - exclude_re is None OR exclude_re does NOT match details_of_condition, AND
#   - forced_category is not already in the selected set.
#
# This table is the single place to add new deterministic routing rules.
# The LLM categoriser sometimes maps terms to a superficially similar category
# (e.g. "aortic" → cardiovascular, "IQ" → cognition) when the schema places
# them elsewhere.  These rules correct that before the second-stage analysis.
# ---------------------------------------------------------------------------
_KEYWORD_CATEGORY_RULES: list[
    tuple[re.Pattern, re.Pattern | None, ConditionCategory, str]
] = [
    # Generic "plegia" (no specific type) → CNS; specific types → musculoskeletal
    (
        re.compile(r"\bplegia\b", re.IGNORECASE),
        re.compile(
            r"\b(paraplegia|quadriplegia|tetraplegia|hemiplegia)\b", re.IGNORECASE
        ),
        ConditionCategory.CNS,
        "generic 'plegia' (unspecified type)",
    ),
    # Cognitive tests and impairment → COGNITION (its main field's description is
    # an instruction, so it contributes no schema terms)
    (
        re.compile(
            r"\bcognitive\b|\bMMSE\b|\bMoCA\b|\bSIMARD\b|\btrails\s+[AB]\b",
            re.IGNORECASE,
        ),
        None,
        ConditionCategory.COGNITION,
        "cognitive impairment / cognitive test",
    ),
    # Aortic dissection → PVD (LLM confuses with cardiovascular aortic conditions)
    (
        re.compile(r"\baortic\s+dissection\b", re.IGNORECASE),
        None,
        ConditionCategory.PVD,
        "aortic dissection",
    ),
    # Carotid stenosis → PVD (LLM confuses "stenosis" with cardiovascular stenosis)
    (
        re.compile(r"\bcarotid\b", re.IGNORECASE),
        None,
        ConditionCategory.PVD,
        "carotid stenosis / carotid condition",
    ),
    # Low IQ / intellectual disability / mental handicap → PSYCHIATRIC
    # (LLM routes to cognition; schema places it in psychiatric.mental_handicap)
    (
        re.compile(
            r"\b(low\s+iq|iq\s+concern|intellectual\s+disabilit|mental\s+handicap)\b",
            re.IGNORECASE,
        ),
        None,
        ConditionCategory.PSYCHIATRIC,
        "low IQ / intellectual disability / mental handicap",
    ),
    # Bare "IQ" mention in free text → PSYCHIATRIC (covers "patient has low iq")
    (
        re.compile(r"\biq\b", re.IGNORECASE),
        None,
        ConditionCategory.PSYCHIATRIC,
        "IQ mention",
    ),
]


def _force_categories_from_keywords(
    slim_json: dict,
    categories: list[ConditionCategory],
    seen: set[ConditionCategory],
) -> None:
    """Deterministically add categories the LLM may misclassify.

    Iterates :data:`_KEYWORD_CATEGORY_RULES` and appends any category whose
    keyword is present (and whose exclusion pattern, if any, is absent) from
    ``details_of_condition``.  Modifies *categories* and *seen* in-place.
    """
    dmer = slim_json.get("dmer", slim_json)
    details = str(dmer.get("details_of_condition", ""))

    for match_re, exclude_re, category, label in _KEYWORD_CATEGORY_RULES:
        if category in seen:
            continue
        if not match_re.search(details):
            continue
        if exclude_re is not None and exclude_re.search(details):
            continue
        _log.info(
            "keyword-forced category", extra={"category": category.value, "rule": label}
        )
        categories.append(category)
        seen.add(category)


def _schema_term_patterns() -> list[tuple[re.Pattern, ConditionCategory, str]]:
    """Whole-word patterns for every condition term the schema names: each
    boolean field's own name and the keywords in its description -- the same
    terms the categorization prompt's field index is built from. A short
    all-caps term (an abbreviation like "MVP") only matches in capitals."""
    patterns = []
    for category, fields in CATEGORY_CONDITIONS.items():
        if category in ALWAYS_ANALYZE_CATEGORIES:
            continue
        terms: set[str] = set()
        for field, cfg in fields.items():
            if cfg["type"] != "bool" or field.endswith(_INDEX_SKIP_SUFFIXES):
                continue
            terms |= _field_terms(field, cfg)
        for term in terms:
            patterns.append((_term_pattern(term), category, term))
    return patterns


def _field_terms(field: str, cfg: dict) -> set[str]:
    """The condition terms a schema field names: its own name (5+ letters)
    and the keywords in its description (an instruction is not a keyword
    list)."""
    terms: set[str] = set()
    local = field.rsplit(".", 1)[-1]
    if local not in _INDEX_SKIP_LOCALS and len(local) >= 5:
        terms.add(local.replace("_", " "))
    desc = cfg.get("description", "").strip()
    if desc and len(desc) <= 120 and not desc.lower().startswith(_INSTRUCTION_STARTERS):
        for kw in desc.split(","):
            kw = kw.strip().rstrip(".")
            if len(kw) >= 4 or (kw.isupper() and len(kw) >= 2):
                terms.add(kw)
    return terms


def _term_pattern(term: str) -> re.Pattern:
    """Whole-word pattern for a schema term; spaces and hyphens are
    interchangeable, an apostrophe optional, and a short all-caps
    abbreviation only matches in capitals."""
    body = r"[\s\-]+".join(re.escape(word) for word in term.split()).replace(
        "\\'", "'?"
    )
    flags = 0 if term.isupper() and len(term) <= 4 else re.IGNORECASE
    return re.compile(rf"\b{body}\b", flags)


_SCHEMA_TERM_PATTERNS = _schema_term_patterns()


def _force_categories_from_schema_terms(
    slim_json: dict,
    categories: list[ConditionCategory],
    seen: set[ConditionCategory],
) -> None:
    """Deterministic routing backstop: add any category whose condition
    terms appear in the written text. Routing a category in only costs an
    analysis call (which still decides); missing one means its conditions
    are never looked for. Modifies *categories* and *seen* in-place."""
    dmer = slim_json.get("dmer", slim_json)
    text = "\n".join(v for v in dmer.values() if isinstance(v, str))
    for pattern, category, term in _SCHEMA_TERM_PATTERNS:
        if category not in seen and pattern.search(text):
            _log.info(
                "schema-term-forced category",
                extra={"category": category.value, "term": term},
            )
            categories.append(category)
            seen.add(category)


def analyze_conditions(openai: OpenAIClient, dmer_json: dict) -> dict:
    """Analyze DMER JSON with LLM — conditions list is in the prompt, not response_format."""
    slim_json = extract_llm_fields(dmer_json)
    _log.info(
        "llm input prepared",
        extra={
            "tracked_field_count": len(EXTRACT_FIELDS),
            "populated_field_count": len(slim_json["dmer"]),
        },
    )

    updates = {"dmer": {}}
    categories = categorize_conditions(openai, slim_json)
    priority_details_signal = _has_priority_details_signal(slim_json)

    for category in categories:
        if category is ConditionCategory.PRIORITY:
            continue
        category_updates = analyze_condition_category(openai, slim_json, category)
        updates["dmer"].update(category_updates.get("dmer", {}))

    if (ConditionCategory.PRIORITY in categories or priority_details_signal) and (
        priority_details_signal or not _has_non_priority_condition_match(updates)
    ):
        category_updates = analyze_condition_category(
            openai,
            slim_json,
            ConditionCategory.PRIORITY,
        )
        updates["dmer"].update(category_updates.get("dmer", {}))

    return updates


def apply_updates(dmer_data: dict, updates: dict) -> dict:
    """Merge LLM updates back into the original DMER JSON."""
    updated = deepcopy(dmer_data)

    if "dmer" not in updated:
        updated["dmer"] = {}

    # Merge LLM results, but never overwrite the source physician narrative.
    filtered_updates = {
        key: value
        for key, value in updates.get("dmer", {}).items()
        if key != "details_of_condition"
    }
    updated["dmer"].update(filtered_updates)

    return updated


# ---------------------------------------------------------------------------
# Deterministic date parsing, with an OCR-digit-confusion fallback
# ---------------------------------------------------------------------------
# dateutil.parser handles every date format actually seen in this dataset
# (e.g. "24APR1932", "14/3/16", "2025-02-05") on the first try. What it
# cannot handle is OCR misreading a digit as a visually similar letter --
# 'O'/'o' for '0', 'I'/'l' for '1' -- which is a real, recurring failure
# mode for handwritten/scanned forms, not a hypothetical one (verified:
# dateutil raises ParserError on "l2-3O-2O25", "O2/O5/2O25", etc.).
_OCR_DIGIT_MAP = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
_ALPHA_RUN_RE = re.compile(r"[A-Za-z]+")
_MONTH_ABBREVIATIONS = frozenset(
    {
        "JAN",
        "FEB",
        "MAR",
        "APR",
        "MAY",
        "JUN",
        "JUL",
        "AUG",
        "SEP",
        "OCT",
        "NOV",
        "DEC",
    }
)


def _fix_ocr_digit_confusion(text: str) -> str:
    """Apply the O/o->0, I/l->1 substitution everywhere *except* inside a
    genuine month abbreviation (e.g. "APR", "FEB") -- those must not be
    corrupted by the same substitution rule that fixes a misread digit.

    A misread digit stuck directly onto a month abbreviation with no
    separator (e.g. "l4MARl6", where the trailing 'l' is a misread '1',
    not part of the month name) is handled by only protecting the first
    three characters of an alphabetic run when they match a known month
    abbreviation, and still translating whatever comes after them in that
    same run.
    """
    pieces: list[str] = []
    last_end = 0
    for match in _ALPHA_RUN_RE.finditer(text):
        pieces.append(text[last_end : match.start()].translate(_OCR_DIGIT_MAP))
        run = match.group()
        if len(run) >= 3 and run[:3].upper() in _MONTH_ABBREVIATIONS:
            pieces.append(run[:3])
            pieces.append(run[3:].translate(_OCR_DIGIT_MAP))
        else:
            pieces.append(run.translate(_OCR_DIGIT_MAP))
        last_end = match.end()
    pieces.append(text[last_end:].translate(_OCR_DIGIT_MAP))
    return "".join(pieces)


# Two defaults that differ in every component: whichever components come out
# different between the two parses were not written in the source text.
_DATE_DEFAULT_A = datetime(2000, 1, 1, tzinfo=UTC)
_DATE_DEFAULT_B = datetime(2004, 12, 28, tzinfo=UTC)


def _parse_date_parts(raw: str) -> tuple[int | None, int | None, int | None] | None:
    """Parse *raw* into (year, month, day), with None for any component
    not present in the text (e.g. "2019" -> (2019, None, None)). Tries
    dateutil as-is first, then with the OCR-confusion fix. Returns None,
    never raises, when neither attempt succeeds.
    """
    for candidate in (raw, _fix_ocr_digit_confusion(raw)):
        try:
            a = dateparser.parse(candidate, dayfirst=False, default=_DATE_DEFAULT_A)
            b = dateparser.parse(candidate, dayfirst=False, default=_DATE_DEFAULT_B)
        except (ValueError, OverflowError, TypeError):
            continue
        return (
            a.year if a.year == b.year else None,
            a.month if a.month == b.month else None,
            a.day if a.day == b.day else None,
        )
    return None


def _latest_possible_date(
    year: int | None, month: int | None, day: int | None, reference: date
) -> date | None:
    """Resolve a possibly-partial date to the MOST RECENT day it could mean.

    More recent onset means higher risk, so an imprecise date must never
    look older than it could be: "2025" -> 2025-12-31, "June 2026" ->
    2026-06-30, "June" (no year) -> the most recent June not after
    *reference*. A partial date is never resolved past *reference*; a
    fully written date is kept as-is.
    Returns None when neither year nor month was written.
    """
    if year is None and month is None:
        return None
    if year is None:
        # Month (and maybe day) only: the most recent occurrence.
        year = reference.year
        if (month, day or 1) > (reference.month, reference.day):
            year -= 1
    last_month = month or 12
    last_day = day or calendar.monthrange(year, last_month)[1]
    try:
        latest = date(year, last_month, last_day)
    except ValueError:
        return None
    if None in (month, day):
        earliest = date(year, month or 1, day or 1)
        if earliest <= reference < latest:
            latest = reference
    return latest


def normalize_dates(dmer_result: dict, *, today: date | None = None) -> dict:
    """Normalize all date fields in the output to YYYY-MM-DD format.

    Scans every key containing 'date' (case-insensitive) and attempts to
    parse the value, falling back to a deterministic OCR-digit-confusion
    fix (see _fix_ocr_digit_confusion) when the raw value doesn't parse
    as-is. Partial dates resolve to the latest possible day, never later
    than *today* (see _latest_possible_date). An unreadable value becomes ""
    (the rule engine accepts a date or "") -- this function never raises.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    today = today or datetime.now(UTC).date()

    def resolve(val: object, reference: date) -> date | None:
        if not isinstance(val, str) or not val.strip():
            return None
        parts = _parse_date_parts(val)
        return _latest_possible_date(*parts, reference) if parts else None

    for key, val in dmer.items():
        if "date" not in key.lower() or key.endswith("_evidence"):
            continue
        resolved = resolve(val, today)
        if resolved is not None:
            dmer[key] = resolved.isoformat()
        elif isinstance(val, str) and val.strip():
            # The rule engine accepts a date or "" only; OCR residue such as
            # ":_._" is not a date.
            _log.warning("unreadable date left blank", extra={"field": key})
            dmer[key] = ""

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


def ensure_all_fields(dmer_result: dict) -> dict:
    """Ensure every field from CONDITIONS is present in the output.

    Fields already set (from input or LLM) are kept as-is.
    Missing fields are filled with a type-appropriate default.
    Any extra fields from the input JSON are also preserved.
    The final output is ordered: original input fields first (in their
    original order), then CONDITIONS fields (in CONDITIONS order).
    """
    dmer = dmer_result.get("dmer", dmer_result)

    for field_name, cfg in CONDITIONS.items():
        if field_name not in dmer:
            dmer[field_name] = TYPE_DEFAULTS.get(cfg["type"], False)

    # Build ordered dict: input fields first, then CONDITIONS-order fields
    conditions_order = list(CONDITIONS.keys())
    conditions_set = set(conditions_order)

    ordered: dict = {}
    # 1. Keep non-CONDITIONS fields from original input in their order
    for key in dmer:
        if key not in conditions_set:
            ordered[key] = dmer[key]
    # 2. Append CONDITIONS fields in canonical order
    for key in conditions_order:
        if key in dmer:
            ordered[key] = dmer[key]

    if "dmer" in dmer_result:
        dmer_result["dmer"] = ordered
    else:
        dmer_result = ordered
    return dmer_result


def flag_concerns(dmer_result: dict) -> dict:
    """Deterministically set ``*_has_concerns`` / ``*_has_concern`` fields.

    For each concern field whose parent condition is active, check whether
    the parent condition's description terms appear in ``details_of_condition``.

    This is a safety-net that runs *after* the LLM merge — it will never
    flip a concern from ``True`` to ``False``, only from ``False`` to ``True``.

    Not called by default from :func:`normalize_document` (matching the
    POC's own default, which left this call commented out in main.py) --
    available for a caller that wants the extra safety net.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    details_lower = str(dmer.get("details_of_condition", "")).lower()

    all_known = set(CONDITIONS.keys()) | set(dmer.keys())

    for field_name in CONDITIONS:
        if not _is_concern_field(field_name):
            continue

        # Already flagged — don't override.
        if dmer.get(field_name) is True:
            continue

        parent = _find_parent_field(field_name, all_known)
        if not parent:
            continue  # category-level concern — leave to LLM

        # Parent condition must be active
        if dmer.get(parent) is not True:
            continue

        # Check if parent's description terms appear in details_of_condition
        if parent in CONDITIONS:
            desc = CONDITIONS[parent].get("description", "")
            for term in (t.strip() for t in desc.split(",") if t.strip()):
                if term.lower() in details_lower:
                    dmer[field_name] = True
                    break

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


# ---------------------------------------------------------------------------
# Deterministic meta fields -- no_other_conditions, guide_with_no_matching_condition
# ---------------------------------------------------------------------------
# Both must run *after* ensure_all_fields, so every CONDITIONS field the LLM
# could have set is already resolved to a real value (not missing) by the
# time these look at it.
# ---------------------------------------------------------------------------


_NO_OTHER_CONDITIONS_EXCLUDED_FIELDS = frozenset(
    {
        # TOP_LEVEL CONDITIONS fields that are administrative/demographic, not
        # a medical condition -- must not make no_other_conditions False just
        # because e.g. a licence class or a restriction code is on file.
        "current_licence_class",
        "blood_pressure",
        "restrictions",
        "medical_examination_date",
        "details_of_condition",  # checked separately, against Section D text
    }
)
# Exam measurements (acuity readings, thresholds, field results), and the
# physician's opinion / recommendations / priority flags and relationship
# details -- none of them name a medical condition. Rules pair several of
# these with no_other_conditions (e.g. "should not drive" or "road test box
# checked" with nothing else on the DMER), so they must not make it False.
_NO_OTHER_CONDITIONS_EXCLUDED_PREFIXES = (
    "visual_acuity.",
    "visual_field.",
    "opinion.",
    "priority.",
    "recommendations.",
    "relationship_with_patient.",
)


def _is_empty_value(value: object) -> bool:
    """True for every shape "not set" can take on a non-bool CONDITIONS
    field: None, an empty/whitespace string, or an empty list -- a bare
    ``value in (None, "", False)`` check misses an empty list, since
    ``[] not in (None, "", False)`` is True in Python (empty containers
    don't equal any of those sentinels).
    """
    if value is None or value is False:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


_PROGRESSIVE_EYE_SOURCE_FIELDS = ("vision.cataracts", "vision.retinopathy")


def flag_progressive_eye_condition(dmer_result: dict) -> dict:
    """Set ``vision.progressive_eye_condition`` true when cataracts or
    retinopathy is true. Never clears an existing true."""
    dmer = dmer_result.get("dmer", dmer_result)
    source = next(
        (f for f in _PROGRESSIVE_EYE_SOURCE_FIELDS if dmer.get(f) is True), None
    )
    if source is not None and dmer.get("vision.progressive_eye_condition") is not True:
        dmer["vision.progressive_eye_condition"] = True
        dmer["vision.progressive_eye_condition_evidence"] = f"{source}: true"
    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


def check_no_other_conditions(dmer_result: dict) -> dict:
    """Sets no_other_conditions = True when nothing was checked and nothing
    was written: every CONDITIONS boolean is false, every CONDITIONS value
    field is empty, and details_of_condition (Section D) itself is blank.

    Administrative/demographic CONDITIONS fields (current_licence_class,
    blood_pressure, restrictions, medical_examination_date) and every
    visual_acuity.* / visual_field.* exam result are excluded -- none of
    them represent a medical condition, so their presence or absence
    doesn't affect whether "no other conditions" is true. `guide`
    isn't excluded here because it isn't in CONDITIONS at all (see
    META_FIELD_DEFAULTS' module comment in schema.py).

    Also sets non_vision_condition_indicated: some condition field outside
    vision.* is active. The rule engine's catch-all ("a condition is on the
    DMER but no chapter rule matched it -> IN") needs this, since the vision
    table fires on acuity alone and a vision-only DMER must not trigger it.
    Section D text alone doesn't count -- the conditions it names are
    extracted into their own fields.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    details = dmer.get("details_of_condition")

    active_fields = []
    for field_name, cfg in CONDITIONS.items():
        if field_name in _NO_OTHER_CONDITIONS_EXCLUDED_FIELDS or field_name.startswith(
            _NO_OTHER_CONDITIONS_EXCLUDED_PREFIXES
        ):
            continue
        value = dmer.get(field_name)
        if (value is True) if cfg["type"] == "bool" else not _is_empty_value(value):
            active_fields.append(field_name)

    dmer["no_other_conditions"] = not active_fields and _is_empty_value(details)
    dmer["non_vision_condition_indicated"] = any(
        not f.startswith("vision.") for f in active_fields
    )

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


_GUIDE_SECTION_NUMBER_RE = re.compile(r"(\d+)")


def parse_guide_section(raw: object) -> int | None:
    """Extract the leading BC Guide section number from the raw guide
    value. Accepts a bare number (e.g. 9.1) or a free-text reference like
    "BC GUIDE 9,1 DIABETES" -- the first integer found is the section
    number. Anything after it (a sub-item number, and/or the condition name
    spelled out) is not parsed here; see check_guide_matching for how the
    condition name is checked.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    match = _GUIDE_SECTION_NUMBER_RE.search(str(raw))
    return int(match.group(1)) if match else None


# Generic catch-all local names that carry no medical meaning of their own
# -- every category has one of these, and matching on the bare word (even
# with a length floor) is unreliable: "other" is a substring of "otherwise",
# "date" of "update", etc. Excluded from the keyword safety net entirely;
# a real signal for these must come through the checkbox/value check above.
_GENERIC_LOCAL_NAMES = frozenset({"other", "date", "cause", "type", "details", "score"})


def _term_matches_details(term: str, details_lower: str) -> bool:
    """Whole-word match, not a bare substring check -- ``"other" in
    "patient otherwise stable"`` is True but wrong; a real keyword match
    must respect word boundaries so a short local name/description term
    can't fire on an unrelated word that merely contains it as a substring.
    """
    if len(term) < 4 or term in _GENERIC_LOCAL_NAMES:
        return False
    return re.search(r"\b" + re.escape(term) + r"\b", details_lower) is not None


def _category_has_active_signal(
    dmer: dict, category: ConditionCategory, details_lower: str
) -> bool:
    """True when *category* already has a real signal in the merged
    output: a true checkbox, a populated value field, or one of its own
    field names/description keywords appearing in details_of_condition.

    The checkbox/value check is the primary signal -- by the time this
    runs, the LLM's own analysis pass has already read Section D and set
    whatever it recognized. The keyword-in-details check is a secondary,
    literal safety net only; it does not re-interpret the free text.
    """
    for field_name, cfg in CATEGORY_CONDITIONS.get(category, {}).items():
        value = dmer.get(field_name)
        if cfg["type"] == "bool":
            if value is True:
                return True
        elif not _is_empty_value(value):
            return True

        local_name = field_name.rsplit(".", 1)[-1].replace("_", " ")
        if _term_matches_details(local_name, details_lower):
            return True

        desc = cfg.get("description", "")
        for term in (t.strip().lower() for t in desc.split(",") if t.strip()):
            if _term_matches_details(term, details_lower):
                return True

    return False


def check_guide_matching(dmer_result: dict) -> dict:
    """Sets guide_with_no_matching_condition: True when a guide reference
    is present but none of the categories that guide section covers
    (GUIDE_SECTION_CATEGORIES) show any active signal -- see
    _category_has_active_signal.

    Semantically, a guide reference means the patient previously had a
    condition under that umbrella (confirmed) -- a historical mention in
    details_of_condition counts as a match, the same as a currently-true
    checkbox; this function doesn't distinguish current vs. past, since
    _category_has_active_signal doesn't either.

    Not every DMER has a guide value; when absent, this defaults to False
    (not flagged) rather than treating "no guide at all" the same as "guide
    present but unmatched". An unrecognized section number (not in
    GUIDE_SECTION_CATEGORIES) is treated the same way -- we can't judge a
    section we don't have mapped, and a false positive on this flag is
    worse than a missed one (same reasoning as duplicate detection's
    conservative comparison-field philosophy -- see
    docs/development/stages/07-decision-gateway.md).
    """
    dmer = dmer_result.get("dmer", dmer_result)
    section = parse_guide_section(dmer.get("guide"))

    if section is None:
        dmer["guide_with_no_matching_condition"] = META_FIELD_DEFAULTS[
            "guide_with_no_matching_condition"
        ]
    else:
        categories = GUIDE_SECTION_CATEGORIES.get(section, ())
        details_lower = str(dmer.get("details_of_condition", "")).lower()
        matched = any(
            _category_has_active_signal(dmer, category, details_lower)
            for category in categories
        )
        dmer["guide_with_no_matching_condition"] = bool(categories) and not matched

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


_GUIDE_SUBSECTION_RE = re.compile(r"(\d+)\.(\d+)([A-Za-z]?)")


def parse_guide_subsection(raw: object) -> str | None:
    """Extract the full BC Guide sub-item token (e.g. "9.1", "9.1A") from
    the raw guide value -- unlike parse_guide_section, a bare section
    number with no sub-item (e.g. just "9") does not match, since there is
    no sub-item to report. Normalizes the letter suffix to uppercase so
    "9.1a" and "9.1A" compare equal.
    """
    if raw is None or raw == "" or isinstance(raw, bool):
        return None
    match = _GUIDE_SUBSECTION_RE.search(str(raw))
    if not match:
        return None
    section, subsection, letter = match.groups()
    return f"{section}.{subsection}{letter.upper()}"


def check_diabetes_guide_9_1(dmer_result: dict) -> dict:
    """Sets diabetes_guide_9_1: True when the raw guide value's BC Guide
    sub-item is 9.1 -- BC Guide Sec. 9 Chapter 7 (Diabetes/Hypoglycemia)
    row 1's "DMER programmed as 9.1" clause. A trailing letter suffix (e.g.
    "9.1A") still counts here: that variant shares the same outcome as
    plain 9.1, so the letter is ignored -- unlike :func:`parse_guide_subsection`,
    which preserves it for callers that do need to tell the variants apart.
    """
    dmer = dmer_result.get("dmer", dmer_result)
    raw = dmer.get("guide")
    match = (
        None
        if raw is None or raw == "" or isinstance(raw, bool)
        else _GUIDE_SUBSECTION_RE.search(str(raw))
    )
    dmer["diabetes_guide_9_1"] = (
        bool(match) and match.group(1) == "9" and match.group(2) == "1"
    )

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


_DIABETES_TREATMENT_CHECKBOX_FIELDS = (
    "endocrine.diabetes.diet",
    "endocrine.diabetes.oral_meds",
    "endocrine.diabetes.insulin",
    "endocrine.diabetes.insulin_secretagogues",
)


def check_diabetes_treatment_not_indicated(dmer_result: dict) -> dict:
    """Sets endocrine.diabetes_treatment_not_indicated: True when none of the
    diabetes treatment checkboxes are set AND Section D text does not
    otherwise mention a diabetes treatment/medication (per the LLM-set
    endocrine.diabetes_treatment_mentioned_in_text) -- BC Guide Sec. 9
    Chapter 7 row 1's "Medication or treatment not indicated on DMER"
    clause. Checkbox absence alone isn't enough: a treatment mentioned only
    in free text, with no matching checkbox marked, must still count as
    "indicated".
    """
    dmer = dmer_result.get("dmer", dmer_result)
    any_checkbox = any(dmer.get(f) is True for f in _DIABETES_TREATMENT_CHECKBOX_FIELDS)
    mentioned_in_text = (
        dmer.get("endocrine.diabetes_treatment_mentioned_in_text") is True
    )
    dmer["endocrine.diabetes_treatment_not_indicated"] = (
        not any_checkbox and not mentioned_in_text
    )

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


_GUIDE_NUMBER_RE = re.compile(r"(\d+)(?:\.(\d+))?")


def parse_guide_number(raw: object) -> int | float | None:
    """The BC Guide reference as the number the rule engine compares against
    (``dmer['guide'] == 17``): "BC GUIDE 17 GENERAL DEBILITY" -> 17,
    "BC GUIDE 9.1A DIABETES" -> 9.1. A letter suffix is dropped -- anything
    that needs it (diabetes_guide_9_1) reads the raw text before this runs.
    None when no number is present.
    """
    if raw is None or isinstance(raw, bool) or raw == "":
        return None
    if isinstance(raw, (int, float)):
        return raw
    match = _GUIDE_NUMBER_RE.search(str(raw))
    if not match:
        return None
    section, subsection = match.groups()
    return float(f"{section}.{subsection}") if subsection else int(section)


_AGE_WORD_RE = re.compile(r"\bage\b", re.IGNORECASE)


def normalize_dmer_type(raw: object) -> str:
    """DI's free-text DMER ``type`` as the rule engine compares it: trimmed
    and lower-case, and "age" for any age-related DMER ("AGE", "Age DMER",
    "age-related") -- the diabetes rules test ``dmer['type'] == 'age'``
    exactly. Blank or missing -> ""."""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text = " ".join(raw.split()).lower()
    return "age" if _AGE_WORD_RE.search(text) else text


# BC licence classes are 1-8; OCR reads 1 as l/I/| and the class box's
# frame or tick as extra 0s/Os (e.g. "100" for a written "1").
# Only a lone l/I/| (not one inside a word such as "Class") is a misread 1.
_LICENCE_CLASS_OCR_RE = re.compile(r"(?<![A-Za-z])[lIi|](?![A-Za-z])")
_LICENCE_CLASS_RE = re.compile(r"[1-8]")


def normalize_licence_class(raw: object) -> str:
    """The licence class(es) in *raw* as "5" or "5,6" -- only the digits
    1-8 are classes, so OCR noise around them (0/O, punctuation, the word
    "class") is dropped. Nothing readable -> ""."""
    if raw is None or isinstance(raw, bool):
        return ""
    text = _LICENCE_CLASS_OCR_RE.sub("1", str(raw))
    classes = list(dict.fromkeys(_LICENCE_CLASS_RE.findall(text)))
    result = ",".join(classes)
    if result != str(raw).strip():
        _log.info("licence class standardized", extra={"readable": bool(result)})
    return result


def apply_rule_engine_input_formats(dmer_result: dict) -> dict:
    """Final shape fixes so the output passes the rule engine's input schema.

    - ``restrictions``: always a list of integer codes, including when the
      field was never set (ensure_all_fields' ``str`` default would be "").
    - ``guide``: a number. The schema allows neither "" nor null, so a DMER
      without a guide number has the key removed rather than blanked.
    - ``type`` and ``current_licence_class``: see :func:`normalize_dmer_type`
      and :func:`normalize_licence_class`.

    Must run after every step that reads the raw guide text
    (check_guide_matching, check_diabetes_guide_9_1).
    """
    dmer = dmer_result.get("dmer", dmer_result)
    dmer["restrictions"] = normalize_restrictions(dmer.get("restrictions"))
    dmer["type"] = normalize_dmer_type(dmer.get("type"))
    if "current_licence_class" in dmer:
        dmer["current_licence_class"] = normalize_licence_class(
            dmer["current_licence_class"]
        )
    guide = parse_guide_number(dmer.get("guide"))
    if guide is None:
        dmer.pop("guide", None)
    else:
        dmer["guide"] = guide

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


def revert_unsupported_concerns(
    dmer: dict, source: dict, updates: dict, flags: list[dict]
) -> None:
    """Set back a concern the analysis model set when it fails an evidence
    check (no evidence, quote not in the source, or not supported).

    A concern belongs to its own condition -- another condition on the DMER
    only counts when the text links them -- and concern flags are where the
    model over-reaches. Conditions and values stay as set (flagged only); a
    concern from the source form, or one the support check could not judge,
    is kept. Every flag is labelled ``action``: "reverted" or "kept".
    """
    reverted = set()
    for flag in flags:
        field = flag["field"]
        if (
            _is_concern_field(field)
            and field in updates
            and flag["check"] != "support_unavailable"
            and dmer.get(field) is True
            and source.get(field) is not True
        ):
            dmer[field] = False
            dmer.pop(f"{field}_evidence", None)
            reverted.add(field)
    for flag in flags:
        flag["action"] = "reverted" if flag["field"] in reverted else "kept"


# Conditions the analysis model reliably fails to return -- some because the
# Azure deployment masks the word and the model then leaves the field out
# (nothing for unmask.py to restore), others when Section D lists several
# conditions and the model skips one. Each is set from the source text
# instead. Acronyms match case-sensitively.
_TEXT_FALLBACKS: dict[str, re.Pattern] = {
    "cns.charcot_marie_tooth_disease": re.compile(
        r"\bCMT\b|\b(?i:charcot[\s-]+marie[\s-]+tooth)\b"
    ),
    "general.crohns": re.compile(r"\bcrohn'?s\b|\bcrohn\b", re.IGNORECASE),
    "cardiovascular.cad": re.compile(
        r"\bCABG\b|\bCAD\b|\b(?i:coronary\s+artery\s+(?:disease|bypass))\b"
    ),
    "vision.cataracts": re.compile(r"\bcataracts?\b", re.IGNORECASE),
    # Not diabetes insipidus or pre-diabetes, which have fields of their own.
    "endocrine.diabetes": re.compile(
        r"\b(?:NIDDM|IDDM|T1DM|T2DM)\b"
        r"|(?<![Pp][Rr][Ee]-)(?<![Pp][Rr][Ee])\b(?i:diabetes)\b(?!\s+(?i:insipidus))"
    ),
    "cardiovascular.mitral_valve_prolapse": re.compile(
        r"\bMVP\b|\b(?i:mitral\s+valve\s+prolapse)\b"
    ),
    "cardiovascular.pacemaker": re.compile(r"\bPPM\b|\b(?i:pace\s?makers?)\b"),
    "general.hernia": re.compile(r"\bhernias?\b", re.IGNORECASE),
    # "C0PD" is a common OCR misread.
    "respiratory.copd": re.compile(
        r"\bC[O0]PD\b|\b(?i:chronic\s+obstructive\s+pulmonary\s+disease)\b"
    ),
    # Not "MS": on this form that is as likely to mean mitral stenosis.
    # Matches a cut-off "multiple scler" too.
    "cns.multiple_sclerosis": re.compile(r"\bmultiple\s+scler\w*", re.IGNORECASE),
    # Masked and dropped when Section D names only this condition.
    "general.colostomy": re.compile(r"\bcolostomy\b", re.IGNORECASE),
    "general.uro": re.compile(r"\buro-?stomy\b", re.IGNORECASE),
    "musculoskeletal.spinal_bifida": re.compile(r"\bspinal?\s+bifida\b", re.IGNORECASE),
    "general.gerd": re.compile(
        r"\bGERD\b|\bGORD\b|\b(?i:gastro-?o?esophageal\s+reflux|acid\s+reflux)\b"
    ),
    "general.cancer": re.compile(r"\b(?:leuka?emia|lymphoma)\b", re.IGNORECASE),
    # "CPAP" is masked; the model sets sleep.cpap but drops its compliance.
    "sleep.cpap_compliant": re.compile(
        r"\bCPAP\b[^.;]{0,30}\bcomplian|\bcomplian\w*\s+(?:with\s+)?(?:\w+\s+)?CPAP\b",
        re.IGNORECASE,
    ),
    # The field's description is longer than a keyword list, so it adds no
    # schema terms; the progressive eye diseases are listed here.
    "vision.progressive_eye_condition": re.compile(
        r"\bAMD\b|\b(?i:macular\s+degeneration|glaucoma|retinitis\s+pigmentosa)\b"
    ),
}
# Schema terms too broad to set the condition on their own.
_FALLBACK_SKIP_TERMS: dict[str, frozenset[str]] = {
    "cns.multiple_sclerosis": frozenset({"MS"}),  # also mitral stenosis
}
# A sentence that matches one of these is about something else.
_FALLBACK_EXCLUDE: dict[str, re.Pattern] = {
    # A stent or angioplasty elsewhere is not coronary.
    "cardiovascular.cad": re.compile(
        r"\b(carotid|femoral|iliac|renal|peripheral|leg|limb)\b", re.IGNORECASE
    ),
    # Non-compliance has its own field.
    "sleep.cpap_compliant": re.compile(
        r"\bnon-?complian|\b(?:poor|partial)(?:ly)?\s+complian", re.IGNORECASE
    ),
    # Malignant hypertension / hyperthermia are not cancer.
    "general.cancer": re.compile(r"\bmalignant\s+hyper", re.IGNORECASE),
    # Diabetes insipidus and pre-diabetes have fields of their own.
    "endocrine.diabetes": re.compile(r"\binsipidus\b|\bpre-?diabet", re.IGNORECASE),
}


def _fallback_patterns() -> dict[str, list[re.Pattern]]:
    """Each fallback field's own pattern (OCR misreads, cut-off words) plus
    every condition term the schema defines for it."""
    patterns = {}
    for field, own in _TEXT_FALLBACKS.items():
        skip = _FALLBACK_SKIP_TERMS.get(field, frozenset())
        terms = sorted(_field_terms(field, CONDITIONS[field]) - skip)
        patterns[field] = [own, *(_term_pattern(term) for term in terms)]
    return patterns


# A sentence that negates or removes the condition doesn't count.
_FALLBACK_NEGATION_RE = re.compile(
    r"\b(no|not|denies|denied|without|negative\s+for|ruled\s+out|r/o|"
    r"removed|explanted|repaired|resolved|family\s+history|fhx?)\b|\?",
    re.IGNORECASE,
)
_SENTENCE_RE = re.compile(r"[^.;\n]+[.;]?")
# Scores written next to their test's name in Section D ("SIMARD 60",
# "MoCA 22/30"), for when the model returns no score.
_SCORE_FALLBACKS: dict[str, re.Pattern] = {
    "cns.simard_score": re.compile(r"\bSIMARD\b\D{0,20}?(-?\d{1,3})\b", re.IGNORECASE),
    "cns.moca_score": re.compile(r"\bMoCA\b\D{0,20}?(\d{1,2})\b", re.IGNORECASE),
    "cns.mmse_score": re.compile(r"\bMMSE\b\D{0,20}?(\d{1,2})\b", re.IGNORECASE),
}
_FALLBACK_REASON = "set from the source text; the model returned nothing for it"


_FALLBACK_PATTERNS = _fallback_patterns()


def _fallback_sources(dmer: dict) -> list[str]:
    """Free-text source fields searched by the text fallbacks."""
    return [
        name
        for name in dmer
        if name == "details_of_condition" or name.endswith(".other")
    ]


def apply_text_fallbacks(dmer: dict, source: dict, updates: dict) -> list[dict]:
    """Set a :data:`_TEXT_FALLBACKS` condition the model returned nothing for
    when the source text names it -- by its own pattern or any term the
    schema defines for it (less :data:`_FALLBACK_SKIP_TERMS`) -- and doesn't
    negate it or match :data:`_FALLBACK_EXCLUDE`. The evidence is
    the source sentence, quoted exactly; its concern stays false (only the
    model judges concerns). A :data:`_SCORE_FALLBACKS` score the model and
    the form both left blank is read from the number after the test's name.
    Returns one ``text_fallback`` flag per field set, so reviewers can see
    the model missed it."""
    flags = []
    for field, patterns in _FALLBACK_PATTERNS.items():
        if field in updates or dmer.get(field) is True:
            continue  # the model (or the form) already decided
        exclude = _FALLBACK_EXCLUDE.get(field)
        for name in _fallback_sources(source):
            text = source.get(name)
            if not isinstance(text, str):
                continue
            sentence = next(
                (
                    s.strip()
                    for s in _SENTENCE_RE.findall(text)
                    if any(p.search(s) for p in patterns)
                    and not _FALLBACK_NEGATION_RE.search(s)
                    and not (exclude and exclude.search(s))
                ),
                None,
            )
            if sentence:
                dmer[field] = True
                dmer[f"{field}_evidence"] = f'{name}: "{sentence}"'
                flags.append(
                    {
                        "field": field,
                        "check": "text_fallback",
                        "reason": _FALLBACK_REASON,
                    }
                )
                _log.info("text fallback set condition", extra={"field": field})
                break
    for field, pattern in _SCORE_FALLBACKS.items():
        # A score the form's own box gave, or the model set, stands. (A null
        # from the model is no score.)
        if not _is_empty_value(dmer.get(field)):
            continue
        for name in _fallback_sources(source):
            text = source.get(name)
            match = pattern.search(text) if isinstance(text, str) else None
            if match:
                dmer[field] = int(match.group(1))
                dmer[f"{field}_evidence"] = f'{name}: "{match.group(0)}"'
                flags.append(
                    {
                        "field": field,
                        "check": "text_fallback",
                        "reason": _FALLBACK_REASON,
                    }
                )
                _log.info("text fallback set score", extra={"field": field})
                break
    return flags


def field_sources(
    source: dict, updates: dict, dmer: dict, flags: list[dict]
) -> dict[str, str]:
    """Where each schema field's final value came from:

    - ``di``: the DI extraction, as read (formats standardized).
    - ``model``: the analysis model.
    - ``model_reverted``: the model set a concern that failed its evidence
      check and was set back to false.
    - ``text_fallback``: set from the source text because the model returned
      nothing for it (:func:`apply_text_fallbacks`).
    - ``rule``: a deterministic rule set or changed it (e.g. an alcohol
      withdrawal seizure -> provoked seizure, no_other_conditions).
    - ``default``: nothing set it; the schema default.
    """
    fallback = {f["field"] for f in flags if f["check"] == "text_fallback"}
    reverted = {f["field"] for f in flags if f.get("action") == "reverted"}
    sources = {}
    for field, cfg in CONDITIONS.items():
        value, is_bool = dmer.get(field), cfg["type"] == "bool"
        if field in fallback:
            origin = "text_fallback"
        elif field in reverted:
            origin = "model_reverted"
        elif field in updates:
            origin = "rule" if is_bool and value != updates[field] else "model"
        elif field in source:
            src = source[field]
            if is_bool:
                origin = "di" if value == src else "rule"
            else:
                origin = (
                    "di" if not _is_blank(src) or _is_empty_value(value) else "rule"
                )
        else:
            origin = "default" if _is_empty_value(value) else "rule"
        sources[field] = origin
    for field in _RULE_OUTPUT_FIELDS:
        if field in dmer:
            sources[field] = "rule"
    return sources


# Outputs that only exist because a deterministic rule computes them.
_RULE_OUTPUT_FIELDS = (
    *META_FIELD_DEFAULTS,
    "diabetes_guide_9_1",
    "endocrine.diabetes_treatment_not_indicated",
)


def missing_evidence(dmer: dict) -> list[str]:
    """True boolean fields without a non-blank ``{field}_evidence`` string.
    Each becomes an evidence flag; the value itself is kept."""
    return [
        field_name
        for field_name, cfg in CONDITIONS.items()
        if cfg["type"] == "bool"
        and dmer.get(field_name) is True
        and (
            not isinstance(dmer.get(f"{field_name}_evidence"), str)
            or not dmer[f"{field_name}_evidence"].strip()
        )
    ]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def normalize_document(
    openai: OpenAIClient, extracted_fields: dict[str, object]
) -> dict:
    """Full Normalize pipeline entry point -- the one function the
    Normalize activity calls.

    *extracted_fields* is di-processor's ``CombinedExtraction.fields``
    (flat, every value a string, checkboxes "selected"/"unselected" -- see
    :func:`adapt_combined_fields`). Returns the final normalized dmer dict,
    unwrapped (not the ``{"dmer": ...}`` envelope) -- the caller decides
    how to store it.

    The two validations in docs/development/stages/04-activity-normalize.md:

    - Schema validation (step 3) applies to the model's output only, per
      call (:func:`accept_analysis_output`). DI input is standardized, never
      validated.
    - Evidence validation (step 4) is per value (:func:`check_evidence`): a
      value that fails is kept and recorded in ``evidence_flags``.

    ``field_sources`` records where each schema field's value came from
    (:func:`field_sources`).

    Raises :class:`NormalizationValidationError` only when a model call's
    output still fails schema validation after its retries -- poison, not
    transient; the caller should route the document to MANUAL_REVIEW.
    """
    dmer_input = flag_monocular_from_bad_eye(adapt_combined_fields(extracted_fields))
    updates = analyze_conditions(openai, dmer_input)
    result = apply_updates(dmer_input, updates)
    fallback_flags = apply_text_fallbacks(
        result["dmer"], dmer_input["dmer"], updates["dmer"]
    )
    result = derive_other_psych_diagnosis(result)
    result = apply_deterministic_field_formats(result)
    result = apply_visual_acuity_thresholds(result)
    result = flag_monocular_from_bad_eye(result)
    result = resolve_conflicts(result)
    result = ensure_all_fields(result)
    result = flag_progressive_eye_condition(result)
    result = check_no_other_conditions(result)
    result = check_guide_matching(result)
    result = check_diabetes_guide_9_1(result)
    result = check_diabetes_treatment_not_indicated(result)
    result = apply_rule_engine_input_formats(result)
    result = normalize_dates(result)
    result = standardize_field_types(result)

    dmer = result["dmer"]
    # Checked source fields already have direct, deterministic provenance.
    # The analysis prompt returns changed fields only, so it need not echo them.
    for field, cfg in CONDITIONS.items():
        if (
            cfg["type"] == "bool"
            and dmer_input["dmer"].get(field) is True
            and dmer.get(field) is True
        ):
            dmer.setdefault(f"{field}_evidence", f"{field}: true")
    flags = [
        {
            "field": field,
            "check": "missing_evidence",
            "reason": "true value has no evidence",
        }
        for field in missing_evidence(dmer)
    ]
    flags += fallback_flags
    flags += check_evidence(openai, dmer_input["dmer"], updates["dmer"], dmer)
    revert_unsupported_concerns(dmer, dmer_input["dmer"], updates["dmer"], flags)
    dmer["evidence_flags"] = flags
    dmer["field_sources"] = field_sources(
        dmer_input["dmer"], updates["dmer"], dmer, flags
    )

    _log.info(
        "normalization complete",
        extra={
            "field_count": len(dmer),
            "evidence_flag_count": len(flags),
            "no_other_conditions": dmer.get("no_other_conditions"),
            "guide_with_no_matching_condition": dmer.get(
                "guide_with_no_matching_condition"
            ),
            "diabetes_guide_9_1": dmer.get("diabetes_guide_9_1"),
            "endocrine.diabetes_treatment_not_indicated": dmer.get(
                "endocrine.diabetes_treatment_not_indicated"
            ),
        },
    )
    return dmer
