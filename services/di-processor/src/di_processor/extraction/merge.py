"""Merge handwritten values into the custom-model extraction (Stage D).

The **custom-model output defines the keys** of the combined result
(checkboxes, identity, dates, and free-text); the handwritten path never adds a
key the custom model did not output.

Overwrite rule, for every handwritten field (the keys in
``dmer_field_schema.json``) that the custom model also output -- decided by the
two confidences:

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
        if _keep_custom_model(hw.confidence, top_level.fields[key].confidence):
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
    handwritten_confidence: Confidence, custom_confidence: float | None
) -> bool:
    """True when the custom-model value is kept instead of the handwritten one:
    the handwritten reading is low confidence and the custom model read the field
    with a confidence above ``CUSTOM_MODEL_KEEP_CONFIDENCE``."""
    if handwritten_confidence is Confidence.HIGH:
        return False
    return (
        custom_confidence is not None
        and custom_confidence > CUSTOM_MODEL_KEEP_CONFIDENCE
    )
