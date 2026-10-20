"""Merge handwritten values into the custom-model extraction (Stage D).

The **custom-model output defines the keys** of the combined result
(checkboxes, identity, dates, and free-text); the handwritten path never adds a
key the custom model did not output.

Overwrite rule: for every handwritten field (the keys in
``dmer_field_schema.json``) that the custom model also output, the **handwritten
(DI OCR + LLM) value replaces the custom-model value**, whatever it is --
including an empty value (the LLM found the field blank, or a safeguard
withheld its reading). The custom-model value of a handwritten field is never
kept. All other custom-model fields pass through unchanged.
"""

from __future__ import annotations

from datetime import UTC, datetime

from .schemas import CombinedExtraction, HandwrittenExtraction, TopLevelExtraction


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

    Starts from all custom-model fields, then overwrites every handwritten field
    that exists in the custom-model output with the handwritten reading, even an
    empty one.
    """
    # 1) Base: every custom-model field (defines the keys).
    fields: dict[str, str] = {name: f.value for name, f in top_level.fields.items()}

    # 2) Overlay: the handwritten reading wins for every existing key, blank or not.
    for key, hw in handwritten.fields.items():
        if key not in fields:
            continue  # handwritten path never introduces new keys
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
