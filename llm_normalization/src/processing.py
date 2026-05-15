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
    ALWAYS_ANALYZE_CATEGORIES,
    CATEGORY_CONDITIONS,
    CATEGORY_PREFIXES,
    CONDITIONS,
    ConditionCategory,
    EXTRACT_FIELDS,
    TYPE_DEFAULTS,
)
from src.prompts import CATEGORY_SYSTEM_PROMPT, build_analysis_prompt


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


def _print_usage(label: str, usage: object) -> None:
    """Print token usage details when the API returns them."""
    if not usage:
        return

    print(f"\n--- Token Usage: {label} ---")
    print(f"  Prompt tokens:     {usage.prompt_tokens:,}")
    if hasattr(usage, "prompt_tokens_details") and usage.prompt_tokens_details:
        cached = getattr(usage.prompt_tokens_details, "cached_tokens", 0)
        if cached:
            print(f"  Cached tokens:     {cached:,}")
    print(f"  Completion tokens: {usage.completion_tokens:,}")
    if hasattr(usage, "completion_tokens_details") and usage.completion_tokens_details:
        reasoning = getattr(usage.completion_tokens_details, "reasoning_tokens", 0)
        if reasoning:
            print(f"  Reasoning tokens:  {reasoning:,}")
    print(f"  Total tokens:      {usage.total_tokens:,}")


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
            print(f"  Ignoring unknown category from LLM: {raw}")
            continue
        if category not in seen:
            categories.append(category)
            seen.add(category)

    for category in ALWAYS_ANALYZE_CATEGORIES:
        if category not in seen:
            categories.append(category)
            seen.add(category)

    return categories


def categorize_conditions(slim_json: dict) -> list[ConditionCategory]:
    """First LLM call: choose which condition categories need analysis."""
    json_str = json.dumps(slim_json, indent=2)

    completion = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {"role": "system", "content": CATEGORY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Categorize this DMER JSON:\n\n{json_str}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    _print_usage("category classification", completion.usage)

    raw = completion.choices[0].message.content
    result = json.loads(raw)
    categories = _parse_categories(result.get("categories", []))
    evidence = result.get("evidence", {})

    print("\n--- LLM selected categories ---")
    print(", ".join(category.value for category in categories) or "(none)")
    if isinstance(evidence, dict) and evidence:
        print("\n--- Category evidence ---")
        for category in categories:
            category_evidence = evidence.get(category.value)
            if category_evidence:
                print(f"  {category.value}: {category_evidence}")

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
    slim_json: dict,
    category: ConditionCategory,
) -> dict:
    """Second LLM call: analyze one category with a narrowed condition list."""
    if not CATEGORY_CONDITIONS[category]:
        return {"dmer": {}}

    category_json = filter_fields_for_category(slim_json, category)
    json_str = json.dumps(category_json, indent=2)
    print("LLM call for category: " + category + "==================================")
    print("input json")
    print(json_str)
    completion = client.chat.completions.create(
        model=DEPLOYMENT,
        messages=[
            {"role": "system", "content": build_analysis_prompt(category)},
            {"role": "user", "content": f"Analyze this DMER JSON:\n\n{json_str}"},
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )

    _print_usage(f"{category.value} analysis", completion.usage)

    raw = completion.choices[0].message.content
    print(raw)
    result = json.loads(raw)

    if "dmer" not in result:
        result = {"dmer": result}

    allowed_fields = set(CATEGORY_CONDITIONS[category])
    filtered: dict = {}
    for field_name, value in result.get("dmer", {}).items():
        evidence_parent = (
            field_name[:-len("_evidence")]
            if field_name.endswith("_evidence")
            else field_name
        )
        if field_name in allowed_fields or evidence_parent in allowed_fields:
            filtered[field_name] = value
        else:
            print(f"  Ignoring field outside {category.value}: {field_name}")

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


def analyze_conditions(dmer_json: dict) -> dict:
    """Analyze DMER JSON with LLM — conditions list is in the prompt, not response_format."""
    slim_json = extract_llm_fields(dmer_json)
    json_str = json.dumps(slim_json, indent=2)
    print(json_str)
    print(
        f"  LLM input: {len(json_str)} chars "
        f"({len(EXTRACT_FIELDS)} fields tracked, {len(slim_json['dmer'])} with values)"
    )

    updates = {"dmer": {}}
    categories = categorize_conditions(slim_json)
    priority_details_signal = _has_priority_details_signal(slim_json)

    for category in categories:
        if category is ConditionCategory.PRIORITY:
            continue
        category_updates = analyze_condition_category(slim_json, category)
        updates["dmer"].update(category_updates.get("dmer", {}))

    if (
        ConditionCategory.PRIORITY in categories
        or priority_details_signal
    ) and (
        priority_details_signal
        or not _has_non_priority_condition_match(updates)
    ):
        category_updates = analyze_condition_category(
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
