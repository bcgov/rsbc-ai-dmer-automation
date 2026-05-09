"""
DMER processing pipeline.
==========================
Functions for extracting, analysing, merging, and normalising DMER
field data through the Azure OpenAI model.
"""

import json
from copy import deepcopy

from dateutil import parser as dateparser

from src.client import client, DEPLOYMENT
from src.conditions import (
    CONDITIONS,
    EXTRACT_FIELDS,
    TYPE_DEFAULTS,
)
from src.prompts import SYSTEM_PROMPT


def _is_concern_field(name: str) -> bool:
    """Return True if *name* is a has_concerns / has_concern flag."""
    return (name.endswith("_has_concerns")
            or name.endswith("_has_concern")
            or name.endswith(".has_concerns"))


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
            if val is None or val == "" or val is False:
                continue
            slim[key] = val

    # 2. Include any boolean field that is True (checked checkboxes)
    for key, val in dmer.items():
        if val is True:
            slim[key] = val

    return {"dmer": slim}


def analyze_conditions(dmer_json: dict) -> dict:
    """Analyze DMER JSON with GPT-4o — conditions list is in the prompt, not response_format."""
    slim_json = extract_llm_fields(dmer_json)
    json_str = json.dumps(slim_json, indent=2)
    print(json_str)
    print(
        f"  LLM input: {len(json_str)} chars "
        f"({len(EXTRACT_FIELDS)} fields tracked, {len(slim_json['dmer'])} with values)"
    )

    completion = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Analyze this DMER JSON:\n\n{json_str}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    # Print token usage
    usage = completion.usage
    if usage:
        print("\n--- Token Usage ---")
        print(f"  Prompt tokens:     {usage.prompt_tokens:,}")
        if hasattr(usage, 'prompt_tokens_details') and usage.prompt_tokens_details:
            cached = getattr(usage.prompt_tokens_details, 'cached_tokens', 0)
            if cached:
                print(f"  Cached tokens:     {cached:,}")
        print(f"  Completion tokens: {usage.completion_tokens:,}")
        if hasattr(usage, 'completion_tokens_details') and usage.completion_tokens_details:
            reasoning = getattr(usage.completion_tokens_details, 'reasoning_tokens', 0)
            if reasoning:
                print(f"  Reasoning tokens:  {reasoning:,}")
        print(f"  Total tokens:      {usage.total_tokens:,}")

    
    raw = completion.choices[0].message.content
    result = json.loads(raw)

    # Ensure we have the dmer wrapper
    if "dmer" not in result:
        result = {"dmer": result}

    return result


def apply_updates(dmer_data: dict, updates: dict) -> dict:
    """Merge LLM updates back into the original DMER JSON."""
    updated = deepcopy(dmer_data)

    if "dmer" not in updated:
        updated["dmer"] = {}

    # Merge — LLM result overwrites matching keys in original
    updated["dmer"].update(updates.get("dmer", {}))

    return updated


def normalize_dates(dmer_result: dict) -> dict:
    """Normalize all date fields in the output to YYYY-MM-DD format.

    Scans every key containing 'date' (case-insensitive) and attempts to
    parse the value into ISO 8601.  Unparseable values are left as-is.
    """
    dmer = dmer_result.get("dmer", dmer_result)

    for key, val in dmer.items():
        if "date" not in key.lower():
            continue
        if not isinstance(val, str) or not val.strip():
            continue
        try:
            parsed = dateparser.parse(val, dayfirst=False)
            dmer[key] = parsed.strftime("%Y-%m-%d")
        except (ValueError, OverflowError, TypeError):
            pass  # leave as-is if unparseable

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
