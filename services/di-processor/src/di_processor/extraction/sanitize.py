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


_CONFIDENCES = frozenset({"high", "low"})
_SOURCES = frozenset({"both", "image", "ocr", "none"})


def _normalize_entry_types(entry: dict[str, Any]) -> None:
    """Coerce one field entry to the schema's types, in place.

    A single odd field (``null`` value, ``"medium"`` confidence, a capitalised
    source) would otherwise fail validation for the whole document.
    """
    value = entry.get("value")
    if value is None:
        entry["value"] = ""
    elif not isinstance(value, str):
        entry["value"] = str(value)
    confidence = str(entry.get("confidence") or "").strip().lower()
    entry["confidence"] = confidence if confidence in _CONFIDENCES else "low"
    source = str(entry.get("source") or "").strip().lower()
    entry["source"] = source if source in _SOURCES else "none"
    notes = entry.get("notes")
    entry["notes"] = "" if notes is None else str(notes)


def sanitize_fields(
    result: dict[str, Any],
    field_keys: tuple[str, ...] | None = None,
    known_labels: frozenset[str] | None = None,
) -> dict[str, Any]:
    """Post-process raw LLM output.

    - Normalize each entry to the schema's types: ``value`` null/number -> string,
      unknown ``confidence`` -> ``low``, unknown ``source`` -> ``none``.
    - Blank any value that matches a printed form label (and flag it ``low``).
    - Force ``low`` confidence whenever ``source != "both"`` -- including BLANK
      fields: the model often marks a field it is sure is empty as ``high`` with
      ``source: none``, which the schema rejects (a real reply has dozens).
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

        _normalize_entry_types(entry)
        value = entry.get("value", "") or ""
        if isinstance(value, str) and _normalize_label(value) in labels and value:
            entry["value"] = ""
            entry["confidence"] = "low"
            note = entry.get("notes", "") or ""
            entry["notes"] = (
                note + " | Auto-corrected: value was a printed form label."
            ).strip(" |")

        if entry["source"] != "both" and entry["confidence"] != "low":
            entry["confidence"] = "low"
            if entry.get("value"):  # a blank field needs no explanation
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
