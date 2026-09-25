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
- Added: schema validation (:func:`validate_schema`) and a same-pass
  evidence-presence check (:func:`validate_evidence_present`), plus
  :func:`adapt_combined_fields` to convert di-processor's real
  ``CombinedExtraction.fields`` shape (flat, every value a string) into the
  typed ``{"dmer": {...}}`` shape the rest of this pipeline expects.
- :func:`normalize_document` is new: the single entry point the Normalize
  activity calls, composing every step below in the right order.

Confirmed contracts this module relies on (see git history / PR discussion
for how these were verified, not assumed):
- Every checkbox field in ``CombinedExtraction.fields`` arrives as the
  canonical string ``"true"``/``"false"`` -- confirmed against a real DI
  response sample and fixed in di-processor's ``top_level.py`` if it
  wasn't already true for a given field's DI configuration.
- MMSE/MoCA scores are written "x/30" on the source form (confirmed
  against docs/development/stages/07-decision-gateway.md's own example).
"""

from __future__ import annotations

import json
import math
import os
import re
from copy import deepcopy
from datetime import datetime

from dateutil import parser as dateparser

from ..openai_client import OpenAIClient
from ..telemetry import get_logger
from .errors import NormalizationValidationError, model_object
from .evidence import validate_derived_values
from .prompts import CATEGORY_SYSTEM_PROMPT, build_analysis_prompt
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
NORMALIZATION_SCHEMA_VERSION = "normalization-schema-v2"

# LLM temperature for each stage -- both default to 0.0 (deterministic
# structured extraction); overridable via env var for experimentation
# without a code change, matching this codebase's general config convention.
CATEGORY_TEMPERATURE = float(os.environ.get("NORMALIZATION_CATEGORY_TEMPERATURE", "0.0"))
ANALYZE_TEMPERATURE = float(os.environ.get("NORMALIZATION_ANALYZE_TEMPERATURE", "0.0"))


# ---------------------------------------------------------------------------
# Input adapter: di-processor's CombinedExtraction.fields -> typed dmer dict
# ---------------------------------------------------------------------------


def adapt_combined_fields(fields: dict[str, object]) -> dict:
    """Convert di-processor's real extraction output shape into the typed
    ``{"dmer": {...}}`` shape the rest of this pipeline expects.

    ``CombinedExtraction.fields`` is flat and every value is a plain string
    (see services/di-processor/.../extraction/schemas.py) -- including
    checkboxes, which arrive as the canonical string "true"/"false" (see
    module docstring). This coerces every field CONDITIONS declares "bool"
    into a real Python bool; everything else passes through unchanged (the
    schema's own str/int/float coercion and the deterministic normalizers
    handle those downstream, the same as they did for the POC's already-
    typed sample data).

    A field this schema doesn't recognize is passed through unchanged --
    permissive, matching ``TopLevelExtraction``'s own "kept permissive so a
    model revision that adds fields does not break parsing" design.
    """
    dmer: dict[str, object] = {}
    for key, value in fields.items():
        cfg = CONDITIONS.get(key)
        if cfg is None or cfg["type"] != "bool":
            dmer[key] = value
            continue
        if isinstance(value, bool):
            dmer[key] = value
        elif isinstance(value, str):
            dmer[key] = value.strip().lower() == "true"
        else:
            dmer[key] = bool(value)
    return {"dmer": dmer}


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
        return None          # suffix matched but parent not found → category-level
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


def categorize_conditions(openai: OpenAIClient, slim_json: dict) -> list[ConditionCategory]:
    """First LLM call: choose which condition categories need analysis."""
    json_str = json.dumps(slim_json, indent=2)

    raw = openai.complete(
        messages=[
            {"role": "system", "content": CATEGORY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Categorize this DMER JSON:\n\n{json_str}"},
        ],
        response_format={"type": "json_object"},
        temperature=CATEGORY_TEMPERATURE,
    )

    result = model_object(raw)
    raw_categories = result.get("categories")
    if not isinstance(raw_categories, list) or any(not isinstance(c, str) for c in raw_categories):
        raise NormalizationValidationError("Normalization categories must be a list of strings")
    categories = _parse_categories(raw_categories)
    seen = set(categories)
    _force_categories_from_keywords(slim_json, categories, seen)

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
                key == prefix or key.startswith(prefix)
                for prefix in cognition_prefixes
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

    raw = openai.complete(
        messages=[
            {"role": "system", "content": build_analysis_prompt(category)},
            {"role": "user", "content": f"Analyze this DMER JSON:\n\n{json_str}"},
        ],
        response_format={"type": "json_object"},
        temperature=ANALYZE_TEMPERATURE,
    )

    result = model_object(raw)

    if "dmer" not in result:
        result = {"dmer": result}
    if not isinstance(result["dmer"], dict):
        raise NormalizationValidationError("Normalization analysis fields must be an object")

    allowed_fields = set(CATEGORY_CONDITIONS[category])
    filtered: dict = {}
    for field_name, value in result.get("dmer", {}).items():
        evidence_parent = field_name.removesuffix("_evidence")
        if field_name in allowed_fields or evidence_parent in allowed_fields:
            filtered[field_name] = value
        else:
            _log.warning(
                "ignoring field outside category",
                extra={"category": category.value},
            )

    return {"dmer": filtered}


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

def resolve_conflicts(dmer_result: dict) -> dict:
    """Apply deterministic field-conflict rules that must not be left to the LLM.

    Called after ``apply_updates`` so every input and LLM-derived field is
    visible in the same dict.

    Rules
    -----
    - ``visual_field.abnormal = True`` → ``vision.field_and_acuity_meet_standard``
      is forced to ``False`` regardless of any free-text statement.  An abnormal
      visual field is a hard disqualifier; the LLM must not override this.
    - ``psychotropic_drugs.alcohol_withdrawal_seizure = True`` → ``cns.epilepsy``
      and ``cns.provoked_seizure`` are forced to ``True``. An alcohol
      withdrawal seizure is still a seizure -- it must not be recorded only
      under its cause and left invisible to every seizure-related field
      that matters for a driving-fitness decision.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    if (dmer.get("visual_field.abnormal") is True
            and dmer.get("vision.field_and_acuity_meet_standard") is not False):
        dmer["vision.field_and_acuity_meet_standard"] = False
        dmer["vision.field_and_acuity_meet_standard_evidence"] = (
            "visual_field.abnormal: true — abnormal visual field overrides "
            "any statement that field and acuity meet standard"
        )

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


def normalize_restrictions(raw: object) -> list:
    """Coerce the restrictions field to a list, whatever shape the source
    JSON gave it in. Real DI extraction can produce a single scalar (an
    int restriction code) or a comma/semicolon-separated string of
    multiple codes -- normalize all shapes to a list.
    """
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, (int, float)):
        return [raw]
    text = str(raw).strip()
    if not text:
        return []
    if "," in text or ";" in text:
        return [part.strip() for part in re.split(r"[,;]", text) if part.strip()]
    return [text]


def apply_deterministic_field_formats(dmer_result: dict) -> dict:
    """Runs parse_nyha_class / parse_cognitive_score / normalize_restrictions
    over the merged output. Called after apply_updates so it sees whatever
    the LLM itself may have already produced for these fields (its own
    "str/int: set to the normalized value" instruction sometimes gets this
    right already -- see prompts.py) -- this pass guarantees the correct
    canonical form regardless, the same way normalize_dates guarantees
    dates regardless of what the LLM attempted on its own.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    if "cardiovascular.nyha_class" in dmer:
        dmer["cardiovascular.nyha_class"] = parse_nyha_class(dmer["cardiovascular.nyha_class"])
    if "cns.mmse_score" in dmer:
        dmer["cns.mmse_score"] = parse_cognitive_score(dmer["cns.mmse_score"])
    if "cns.moca_score" in dmer:
        dmer["cns.moca_score"] = parse_cognitive_score(dmer["cns.moca_score"])
    if "restrictions" in dmer:
        dmer["restrictions"] = normalize_restrictions(dmer["restrictions"])

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
_KEYWORD_CATEGORY_RULES: list[tuple[re.Pattern, re.Pattern | None, ConditionCategory, str]] = [
    # Generic "plegia" (no specific type) → CNS; specific types → musculoskeletal
    (
        re.compile(r"\bplegia\b", re.IGNORECASE),
        re.compile(r"\b(paraplegia|quadriplegia|tetraplegia|hemiplegia)\b", re.IGNORECASE),
        ConditionCategory.CNS,
        "generic 'plegia' (unspecified type)",
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
        _log.info("keyword-forced category", extra={"category": category.value, "rule": label})
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

    if (
        ConditionCategory.PRIORITY in categories
        or priority_details_signal
    ) and (
        priority_details_signal
        or not _has_non_priority_condition_match(updates)
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
_MONTH_ABBREVIATIONS = frozenset({
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
})


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
        pieces.append(text[last_end:match.start()].translate(_OCR_DIGIT_MAP))
        run = match.group()
        if len(run) >= 3 and run[:3].upper() in _MONTH_ABBREVIATIONS:
            pieces.append(run[:3])
            pieces.append(run[3:].translate(_OCR_DIGIT_MAP))
        else:
            pieces.append(run.translate(_OCR_DIGIT_MAP))
        last_end = match.end()
    pieces.append(text[last_end:].translate(_OCR_DIGIT_MAP))
    return "".join(pieces)


def _parse_date_with_ocr_fallback(raw: str) -> datetime | None:
    """Try dateutil as-is first; only apply the OCR-confusion fix (and
    re-attempt) if the first parse fails. Returns None, never raises, when
    neither attempt succeeds -- caller decides what to do with that (this
    module leaves the original value in place, per normalize_dates' own
    documented behaviour).
    """
    for candidate in (raw, _fix_ocr_digit_confusion(raw)):
        try:
            return dateparser.parse(candidate, dayfirst=False)
        except (ValueError, OverflowError, TypeError):
            continue
    return None


def normalize_dates(dmer_result: dict) -> dict:
    """Normalize all date fields in the output to YYYY-MM-DD format.

    Scans every key containing 'date' (case-insensitive) and attempts to
    parse the value into ISO 8601, falling back to a deterministic
    OCR-digit-confusion fix (see _fix_ocr_digit_confusion) when the raw
    value doesn't parse as-is. Unparseable values (even after the fallback)
    are left as-is -- this function never raises and never guesses.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    for key, val in dmer.items():
        if "date" not in key.lower():
            continue
        if not isinstance(val, str) or not val.strip():
            continue
        parsed = _parse_date_with_ocr_fallback(val)
        if parsed is not None:
            dmer[key] = parsed.strftime("%Y-%m-%d")

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
            continue   # category-level concern — leave to LLM

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


_NO_OTHER_CONDITIONS_EXCLUDED_FIELDS = frozenset({
    # TOP_LEVEL CONDITIONS fields that are administrative/demographic, not
    # a medical condition -- must not make no_other_conditions False just
    # because e.g. a licence class or a restriction code is on file.
    "current_licence_class",
    "blood_pressure",
    "restrictions",
    "medical_examination_date",
    "details_of_condition",  # checked separately, against Section D text
})


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


def check_no_other_conditions(dmer_result: dict) -> dict:
    """Sets no_other_conditions = True when nothing was checked and nothing
    was written: every CONDITIONS boolean is false, every CONDITIONS value
    field is empty, and details_of_condition (Section D) itself is blank.

    Administrative/demographic CONDITIONS fields (current_licence_class,
    blood_pressure, restrictions, medical_examination_date) are excluded --
    none of them represent a medical condition, so their presence or
    absence doesn't affect whether "no other conditions" is true. `guide`
    isn't excluded here because it isn't in CONDITIONS at all (see
    META_FIELD_DEFAULTS' module comment in schema.py).
    """
    dmer = dmer_result.get("dmer", dmer_result)
    details = dmer.get("details_of_condition")

    any_condition_active = False
    for field_name, cfg in CONDITIONS.items():
        if field_name in _NO_OTHER_CONDITIONS_EXCLUDED_FIELDS:
            continue
        value = dmer.get(field_name)
        if cfg["type"] == "bool":
            if value is True:
                any_condition_active = True
                break
        elif not _is_empty_value(value):
            any_condition_active = True
            break

    dmer["no_other_conditions"] = not any_condition_active and _is_empty_value(details)

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
    dmer["diabetes_guide_9_1"] = bool(match) and match.group(1) == "9" and match.group(2) == "1"

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
    """Sets diabetes_treatment_not_indicated: True when none of the
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
    mentioned_in_text = dmer.get("endocrine.diabetes_treatment_mentioned_in_text") is True
    dmer["diabetes_treatment_not_indicated"] = not any_checkbox and not mentioned_in_text

    if "dmer" in dmer_result:
        dmer_result["dmer"] = dmer
    return dmer_result


# ---------------------------------------------------------------------------
# Output validation
# ---------------------------------------------------------------------------


# Fields whose CONDITIONS "type" doesn't reflect their final, post-
# deterministic-normalization shape. "restrictions" is declared "str" in
# the schema (matching what the LLM sees it as during analysis -- it's a
# TOP_LEVEL field the analysis prompt can "normalize" like any other str
# field), but apply_deterministic_field_formats always overrides it to a
# list afterward (see normalize_restrictions) -- that list is the correct,
# intended final shape, not a violation. Found by running normalize_document
# against a real LLM call, not written speculatively: a synthetic unit
# test of validate_schema alone (never exercising
# apply_deterministic_field_formats) didn't surface this.
_SCHEMA_TYPE_OVERRIDES: dict[str, tuple[type, ...]] = {
    "restrictions": (str, list),
}


def validate_schema(dmer: dict) -> None:
    """Raise :class:`NormalizationValidationError` if any CONDITIONS
    field's final value doesn't match its declared type.

    The deterministic passes above (apply_deterministic_field_formats,
    ensure_all_fields, adapt_combined_fields, ...) should already guarantee
    this for every field they touch; this is the final backstop for a
    genuinely malformed LLM response that slipped through both LLM calls
    untouched by any of them.
    """
    violations: list[str] = []
    for field_name, cfg in CONDITIONS.items():
        value = dmer.get(field_name)
        override = _SCHEMA_TYPE_OVERRIDES.get(field_name)
        if override is not None:
            if not isinstance(value, override):
                expected = " or ".join(t.__name__ for t in override)
                violations.append(f"{field_name}: expected {expected}, got {type(value).__name__}")
            continue
        field_type = cfg["type"]
        if field_type == "bool":
            if not isinstance(value, bool):
                violations.append(f"{field_name}: expected bool, got {type(value).__name__}")
        elif field_type == "str":
            if not isinstance(value, str):
                violations.append(f"{field_name}: expected str, got {type(value).__name__}")
        elif field_type in ("int", "float"):
            allowed_types = (int,) if field_type == "int" else (int, float)
            if value is not None and (
                type(value) not in allowed_types
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                violations.append(
                    f"{field_name}: expected {field_type} or null, got {type(value).__name__}"
                )
    if violations:
        raise NormalizationValidationError(
            f"{len(violations)} field(s) failed schema validation: "
            + "; ".join(violations[:10])
        )


def validate_evidence_present(dmer: dict) -> None:
    """Raise :class:`NormalizationValidationError` if a true boolean field
    is missing its ``{field}_evidence`` non-blank string.

    This is the deterministic presence/type check. Source support for
    model-derived values is checked separately by validate_derived_values.
    """
    missing = [
        field_name
        for field_name, cfg in CONDITIONS.items()
        if cfg["type"] == "bool"
        and dmer.get(field_name) is True
        and (
            not isinstance(dmer.get(f"{field_name}_evidence"), str)
            or not dmer[f"{field_name}_evidence"].strip()
        )
    ]
    if missing:
        raise NormalizationValidationError(
            f"{len(missing)} true field(s) missing required evidence: "
            + ", ".join(missing[:10])
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def normalize_document(openai: OpenAIClient, extracted_fields: dict[str, object]) -> dict:
    """Full Normalize pipeline entry point -- the one function the
    Normalize activity calls.

    *extracted_fields* is di-processor's ``CombinedExtraction.fields``
    (flat, every checkbox a canonical "true"/"false" string -- see
    :func:`adapt_combined_fields`). Returns the final normalized dmer dict,
    unwrapped (not the ``{"dmer": ...}`` envelope) -- the caller decides
    how to store it.

    Raises :class:`NormalizationValidationError` on a schema or evidence
    violation -- poison, not transient (see failure-handling in
    docs/development/stages/04-activity-normalize.md); the caller should
    route the document to MANUAL_REVIEW rather than retry.
    """
    dmer_input = adapt_combined_fields(extracted_fields)
    updates = analyze_conditions(openai, dmer_input)
    result = apply_updates(dmer_input, updates)
    result = apply_deterministic_field_formats(result)
    result = resolve_conflicts(result)
    result = ensure_all_fields(result)
    result = check_no_other_conditions(result)
    result = check_guide_matching(result)
    result = check_diabetes_guide_9_1(result)
    result = check_diabetes_treatment_not_indicated(result)
    result = normalize_dates(result)

    dmer = result["dmer"]
    # Checked source fields already have direct, deterministic provenance.
    # The analysis prompt returns changed fields only, so it need not echo them.
    for field, cfg in CONDITIONS.items():
        if cfg["type"] == "bool" and dmer_input["dmer"].get(field) is True and dmer.get(field) is True:
            dmer.setdefault(f"{field}_evidence", f"{field}: true")
    validate_schema(dmer)
    validate_evidence_present(dmer)
    validate_derived_values(openai, dmer_input["dmer"], updates["dmer"], dmer)

    _log.info(
        "normalization complete",
        extra={
            "field_count": len(dmer),
            "no_other_conditions": dmer.get("no_other_conditions"),
            "guide_with_no_matching_condition": dmer.get("guide_with_no_matching_condition"),
            "diabetes_guide_9_1": dmer.get("diabetes_guide_9_1"),
            "diabetes_treatment_not_indicated": dmer.get("diabetes_treatment_not_indicated"),
        },
    )
    return dmer
