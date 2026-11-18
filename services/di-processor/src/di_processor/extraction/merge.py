"""Merge handwritten values into the custom-model extraction (Stage D).

The **custom-model output defines the keys** of the combined result
(checkboxes, identity, dates, and free-text); the handwritten path never adds a
key the custom model did not output.

Overwrite rule, for every handwritten field (the keys in
``dmer_field_schema.json``) that the custom model also output -- decided by the
two confidences:

- a visual acuity field (``VISUAL_ACUITY_FIELDS``) the custom model read with
  confidence ``VISUAL_ACUITY_KEEP_CONFIDENCE`` (0.95) or above -> the
  custom-model value, whatever the handwritten reading, an empty value included;
- handwritten (DI OCR + LLM) confidence ``high`` -> the handwritten value;
- handwritten confidence ``low``:
  - custom-model confidence above ``CUSTOM_MODEL_KEEP_CONFIDENCE`` (0.75) -> the
    custom-model value is kept;
  - otherwise (0.75 or below, or no confidence) -> the handwritten value.

The handwritten value used is whatever it is, including an empty value (the LLM
found the field blank, or a safeguard such as the MoCA label check withheld its
reading). All other custom-model fields pass through unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .schemas import (
    CombinedExtraction,
    Confidence,
    HandwrittenExtraction,
    TopLevelExtraction,
)

# A low-confidence handwritten reading does not replace a custom-model value read
# with a confidence above this.
CUSTOM_MODEL_KEEP_CONFIDENCE = 0.75

# The visual acuity grid: a custom-model reading at or above
# VISUAL_ACUITY_KEEP_CONFIDENCE is kept over any handwritten reading. The
# handwriting LLM reads this grid inconsistently between runs (values swapped
# between rows, or one row's values copied into another).
VISUAL_ACUITY_FIELDS = frozenset(
    f"visual_acuity.{correction}_{eye}"
    for correction in ("corrected", "uncorrected")
    for eye in ("left", "right", "both")
)
VISUAL_ACUITY_KEEP_CONFIDENCE = 0.95


def merge(
    document_id: str,
    top_level: TopLevelExtraction,
    handwritten: HandwrittenExtraction,
    *,
    source_model_version: str | None = None,
    prompt_version: str | None = None,
    processed_at: datetime | None = None,
) -> CombinedExtraction:
    """Merge handwritten values onto the custom-model base.

    Starts from all custom-model fields, then for every handwritten field that
    exists in the custom-model output picks one value by the confidences (see the
    module docstring).
    """
    # 1) Base: every custom-model field (defines the keys).
    fields: dict[str, str] = {name: f.value for name, f in top_level.fields.items()}

    # 2) Overlay: per field, the handwritten or the custom-model value.
    for key, hw in handwritten.fields.items():
        if key not in fields:
            continue  # handwritten path never introduces new keys
        if _keep_custom_model(key, hw.confidence, top_level.fields[key].confidence):
            continue
        fields[key] = hw.value

    return CombinedExtraction(
        document_id=document_id,
        source_model_version=source_model_version,
        prompt_version=prompt_version,
        processed_at=processed_at or datetime.now(UTC),
        fields=fields,
        uncertain_fields=list(handwritten.uncertain_fields),
        cutoff=top_level.cutoff,
    )


def _keep_custom_model(
    key: str, handwritten_confidence: Confidence, custom_confidence: float | None
) -> bool:
    """True when the custom-model value is kept instead of the handwritten one:
    a visual acuity field the custom model read with a confidence of
    ``VISUAL_ACUITY_KEEP_CONFIDENCE`` or above, or a low-confidence handwritten
    reading of a field the custom model read with a confidence above
    ``CUSTOM_MODEL_KEEP_CONFIDENCE``."""
    if (
        key in VISUAL_ACUITY_FIELDS
        and custom_confidence is not None
        and custom_confidence >= VISUAL_ACUITY_KEEP_CONFIDENCE
    ):
        return True
    if handwritten_confidence is Confidence.HIGH:
        return False
    return (
        custom_confidence is not None
        and custom_confidence > CUSTOM_MODEL_KEEP_CONFIDENCE
    )
