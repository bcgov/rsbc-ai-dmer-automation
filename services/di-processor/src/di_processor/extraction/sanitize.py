"""LLM output parsing + post-processing safeguards (ported from the POC).

Pure functions (no I/O beyond reading bundled resource files):

- :func:`parse_llm_json` / :func:`repair_json_text` — tolerate markdown fences and
  repair known LLM JSON defects (JS ternary values) before parsing.
- :func:`sanitize_fields` — fill any missing field keys, blank values that are
  actually printed form labels, and force ``low`` confidence whenever the two
  sources did not both agree (the binary-confidence rule).
- :func:`check_label_anchors` — blank a score the OCR shows next to a different
  label (e.g. a visual acuity "20/30" read as the MoCA score).

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


# Score fields checked against the OCR: the field's printed label (lowercase,
# letters only) as it appears in the OCR text.
_ANCHOR_LABELS: dict[str, str] = {"cns.moca_score": "moca"}
_SPACES = re.compile(r"\s+")


def _ocr_segment_texts(ocr_json: dict[str, Any]) -> list[str]:
    return [
        segment.get("text") or ""
        for page in ocr_json.get("pages") or []
        for row in page.get("rows") or []
        for segment in row.get("segments") or []
    ]


def check_label_anchors(
    result: dict[str, Any], ocr_json: dict[str, Any]
) -> dict[str, Any]:
    """Blank a score the OCR shows only next to a different label.

    For each field in ``_ANCHOR_LABELS``: if its value appears in the OCR, at
    least one OCR segment holding the value must also hold the field's label.
    A value found only in segments without the label was taken from another
    part of the form (DMER21: a visual acuity "20/30", OCR'd as "29/30", came
    back as the MoCA score), so it is blanked with a note. A value the OCR does
    not show at all is left alone: that is an image-only reading, already low.

    Returns the mutated ``result`` for convenience.
    """
    segments = [_SPACES.sub("", text).lower() for text in _ocr_segment_texts(ocr_json)]
    fields = result.get("fields") or {}
    for key, label in _ANCHOR_LABELS.items():
        entry = fields.get(key)
        if not isinstance(entry, dict):
            continue
        value = _SPACES.sub("", str(entry.get("value") or "")).lower()
        if not value:
            continue
        holding = [text for text in segments if value in text]
        if holding and not any(label in text for text in holding):
            entry["value"] = ""
            entry["confidence"] = "low"
            note = entry.get("notes", "") or ""
            entry["notes"] = (
                note
                + f" | Value withheld: the OCR shows it only away from the {label.upper()} label."
            ).strip(" |")
    return result
