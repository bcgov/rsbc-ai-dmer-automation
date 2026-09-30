"""Cut-off detection for a faxed/scanned DMER page (Stage A post-step).

Determines whether the top (header) and bottom (examiner / signature) bands of
the MV2011C form survived the fax/scan, from the layout the custom-model call
already returned — no extra Document Intelligence call and no LLM (see
``docs/development/stages/02-extraction.md`` §Cut-off detection in practice).

The custom-model result is used rather than the tiled OCR because the tiled path
runs on an image with the top Protected B band cropped off (``render.py``), so
it can never see the header.

Two kinds of evidence:

- **Printed anchors** — form labels at fixed relative positions, fuzzy-matched
  against the page's DI lines (fax OCR is noisy, e.g. "REI ATIONCHID WITH
  DATIENT"), and only counted inside their expected vertical band.
- **Custom-model fields** (bottom band only) — the examiner-block fields the
  model is trained on. A field counts only when it was actually located (has a
  value or a bounding region); an absent field still reports a high
  ``confidence`` (the model is confident it is empty), so confidence is ignored.

The fax-machine banner at the very top ("03/06/2024 WED 16:29 FAX ...") is not
a form element and is deliberately not an anchor.

``has_header`` / ``has_signature`` / ``is_cutoff`` stay three separate flags —
Intake needs to know which half of the form is missing. A page with no layout
lines yields all three as ``None`` (undeterminable), never a guessed ``False``.

Pure functions over plain dicts (the :class:`~dmer_common.doc_intelligence.DIResult`
shape), unit-testable without Azure.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .schemas import CutoffFlags

# Printed form labels in the top band (above the Protected B office area).
HEADER_ANCHORS: tuple[str, ...] = (
    "DRIVER'S MEDICAL EXAMINATION",
    "RoadSafetyBC",
    "AREA ABOVE FOR OFFICE USE",
)
# Printed form labels in the bottom examiner / signature / submission block.
SIGNATURE_ANCHORS: tuple[str, ...] = (
    "EXAMINING PHYSICIAN'S OR NP'S NAME AND ADDRESS",
    "Examination Date",
    "Physician's or NP's Signature",
    "TELEPHONE NO.",
    "PHYSICIAN OR NP: FAX TO 250-952-6888 OR MAIL TO RoadSafetyBC",
)
# Custom-model fields that live in the bottom examiner / signature block.
SIGNATURE_FIELDS: tuple[str, ...] = (
    "doctor_signature",
    "medical_examination_date",
    "physician_or_np_fax_present",
)

# Header anchors must start in the top HEADER_MAX_Y of the page; signature
# anchors at or below SIGNATURE_MIN_Y. Guards against a label matching
# similar text elsewhere on the page.
HEADER_MAX_Y = 0.20
SIGNATURE_MIN_Y = 0.60
# A band is present when at least this many of its anchors are found.
MIN_ANCHORS = 2
# Fuzzy-match threshold (0..1) between an anchor and a line window.
MATCH_THRESHOLD = 0.80

_NON_ALNUM = re.compile(r"[^0-9a-z]+")
# DI field keys that carry an extracted value (besides ``content``).
_VALUE_KEY_PREFIX = "value"


def _normalize(text: str) -> str:
    """Casefold and collapse punctuation/whitespace so OCR noise matters less."""
    return _NON_ALNUM.sub(" ", text.casefold()).strip()


def _similarity(anchor: str, line: str) -> float:
    """Best fuzzy score of ``anchor`` against any same-length word window of ``line``.

    Windows let a short anchor match inside a longer line (e.g. the fax footer
    line carries more text than the anchor) without full substring search.
    """
    a = _normalize(anchor)
    words = _normalize(line).split()
    if not a or not words:
        return 0.0
    n = len(a.split())
    best = 0.0
    for size in {max(n - 1, 1), n, n + 1}:
        for start in range(max(len(words) - size + 1, 1)):
            window = " ".join(words[start : start + size])
            best = max(best, SequenceMatcher(None, a, window).ratio())
            if best == 1.0:
                return best
    return best


def _lines(page: dict[str, Any]) -> list[tuple[str, float]]:
    """Return ``(content, y_top_ratio)`` for each DI line on ``page``."""
    height = page.get("height") or 0
    if height <= 0:
        return []
    out: list[tuple[str, float]] = []
    for line in page.get("lines") or []:
        polygon = line.get("polygon") or []
        ys = polygon[1::2]
        if not ys:
            continue
        out.append((line.get("content", ""), min(ys) / height))
    return out


def matched_anchors(
    lines: list[tuple[str, float]],
    anchors: tuple[str, ...],
    *,
    min_y: float = 0.0,
    max_y: float = 1.0,
    threshold: float = MATCH_THRESHOLD,
) -> list[str]:
    """Return the anchors found in a line whose top lies within ``[min_y, max_y]``."""
    in_band = [text for text, y in lines if min_y <= y <= max_y]
    return [
        anchor
        for anchor in anchors
        if any(_similarity(anchor, text) >= threshold for text in in_band)
    ]


def _field_located(raw: Any) -> bool:
    """True when a DI field was actually found (a value or a bounding region)."""
    if not isinstance(raw, dict):
        return False
    if raw.get("boundingRegions") or raw.get("content"):
        return True
    return any(
        key.startswith(_VALUE_KEY_PREFIX) and value not in (None, "")
        for key, value in raw.items()
    )


def located_fields(fields: dict[str, Any], names: tuple[str, ...]) -> list[str]:
    """Return the ``names`` present in the custom-model ``fields`` output."""
    return [name for name in names if _field_located(fields.get(name))]


def detect_cutoff(page: dict[str, Any] | None, fields: dict[str, Any]) -> CutoffFlags:
    """Compute the three cut-off flags for page 1.

    ``page`` is ``DIResult.pages[0]`` and ``fields`` the first document's
    ``fields`` from the same custom-model call.
    """
    lines = _lines(page or {})
    if not lines:
        return CutoffFlags()

    header = matched_anchors(lines, HEADER_ANCHORS, max_y=HEADER_MAX_Y)
    signature = matched_anchors(lines, SIGNATURE_ANCHORS, min_y=SIGNATURE_MIN_Y)
    signature_fields = located_fields(fields, SIGNATURE_FIELDS)

    has_header = len(header) >= MIN_ANCHORS
    has_signature = len(signature) >= MIN_ANCHORS or bool(signature_fields)
    return CutoffFlags(
        has_header=has_header,
        has_signature=has_signature,
        is_cutoff=not (has_header and has_signature),
        header_anchors=header,
        signature_anchors=signature,
        signature_fields=signature_fields,
    )
