"""Licence number as read from the page (``dmer_extraction.licence_number_read``).

Taken from the custom-model ``dl_number`` field (the licence band sits in the
Protected B area, which the custom model sees because it analyzes the uncropped
page). Normalized with :func:`dmer_common.licence.normalize_licence` so it
matches ``driver.licence_number`` exactly.

Recorded as ``None`` only when the field is absent or the value is not a valid
BC licence (7 or 8 digits). Model confidence does not gate the read; it stays
alongside the value in the extraction blobs for downstream stages to weigh.

The mismatch check against a Mercury-supplied driver belongs to driver
resolution, not here — this only records what the page says. Never log the
value (PII); log whether one was read.
"""

from __future__ import annotations

from dmer_common.licence import normalize_licence

from .schemas import TopLevelExtraction

LICENCE_FIELD = "dl_number"


def read_licence(top: TopLevelExtraction) -> str | None:
    """Return the canonical licence from the custom-model output, or None."""
    field = top.fields.get(LICENCE_FIELD)
    if field is None:
        return None
    return normalize_licence(field.value)
