"""LLM output parsing + post-processing safeguards (ported from the POC).

Pure functions (no I/O beyond reading bundled resource files):

- :func:`parse_llm_json` / :func:`repair_json_text` — tolerate markdown fences and
  repair known LLM JSON defects (JS ternary values) before parsing.
- :func:`sanitize_fields` — fill any missing field keys, blank values that are
  actually printed form labels, and force ``low`` confidence whenever the two
  sources did not both agree (the binary-confidence rule).

Resource loaders read the bundled ``dmer_field_schema.json`` and
``dmer_template_labels.json``.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from importlib import resources
from typing import Any

_RESOURCES = "di_processor.extraction.resources"
_TERNARY = re.compile(
    r':\s*[^,{}\[\]]*?\?\s*("(?:[^"\\]|\\.)*")\s*:\s*"(?:[^"\\]|\\.)*"'
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _read_resource(name: str) -> str:
    return resources.files(_RESOURCES).joinpath(name).read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def load_field_keys() -> tuple[str, ...]:
    """Return the fixed list of handwritten field keys."""
    data = json.loads(_read_resource("dmer_field_schema.json"))
    return tuple(data.get("fields", []))


def _normalize_label(text: str) -> str:
    """Lowercase and strip all non-alphanumerics for robust label comparison."""
    return _NON_ALNUM.sub("", text.lower())


@lru_cache(maxsize=1)
def load_known_form_labels() -> frozenset[str]:
    """Return the normalized set of printed template labels."""
    data = json.loads(_read_resource("dmer_template_labels.json"))
    labels: set[str] = set()
    for lbl in data.get("all_labels", []):
        norm = _normalize_label(lbl)
        if norm:
            labels.add(norm)
    for section in data.get("sections", []):
        for lbl in section.get("labels", []):
            norm = _normalize_label(lbl)
            if norm:
                labels.add(norm)
    return frozenset(labels)


def load_prompt() -> str:
    """Return the LLM reconstruction system prompt."""
    return _read_resource("llm_prompt_schema.md")


def repair_json_text(text: str) -> str:
    """Collapse JS ternary values (``<expr> ? "A" : "B"`` -> ``"A"``) to valid JSON."""
    return _TERNARY.sub(r": \1", text)


def parse_llm_json(text: str) -> dict[str, Any]:
    """Strip markdown fences, repair known defects, and parse to a dict."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return json.loads(repair_json_text(stripped))


def sanitize_fields(
    result: dict[str, Any],
    field_keys: tuple[str, ...] | None = None,
    known_labels: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Post-process raw LLM output.

    - Blank any value that matches a printed form label (and flag it ``low``).
    - Force ``low`` confidence whenever ``source != "both"``.
    - Add any missing field keys as empty ``low``/``none`` entries.

    Returns the mutated ``result`` for convenience.
    """
    keys = field_keys if field_keys is not None else load_field_keys()
    labels = known_labels if known_labels is not None else load_known_form_labels()

    fields = result.get("fields")
    if not isinstance(fields, dict):
        fields = {}
        result["fields"] = fields

    for entry in fields.values():
        if not isinstance(entry, dict):
            continue

        value = entry.get("value", "") or ""
        if isinstance(value, str) and _normalize_label(value) in labels and value:
            entry["value"] = ""
            entry["confidence"] = "low"
            note = entry.get("notes", "") or ""
            entry["notes"] = (
                note + " | Auto-corrected: value was a printed form label."
            ).strip(" |")

        source = str(entry.get("source", "")).lower()
        if (
            (entry.get("value") or "")
            and source != "both"
            and entry.get("confidence") != "low"
        ):
            entry["confidence"] = "low"
            note = entry.get("notes", "") or ""
            entry["notes"] = (
                note + " | Confidence set to low: image and OCR did not both agree."
            ).strip(" |")

    for key in keys:
        if key not in fields:
            fields[key] = {
                "value": "",
                "confidence": "low",
                "source": "none",
                "notes": "Field missing from LLM output; defaulted to empty.",
            }

    return result
