"""Merge handwritten values into the custom-model extraction (Stage D).

The **custom-model output is authoritative and complete**: it defines every key
in the combined result (checkboxes, identity, dates, and free-text). The
handwritten path (DI OCR + LLM) is used **only to fill/refine the handwritten
values** of keys that already exist in the custom-model output — it never adds
new keys and never blanks a custom-model value.

Overwrite rule: a handwritten field replaces the custom-model value only when
the handwritten path actually captured content (``source != "none"`` and a
non-empty value). Blank handwritten results (``source == "none"``) leave the
custom-model value untouched, so we never destroy a value the custom model read.
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

    Starts from all custom-model fields, then overwrites a field's value with the
    handwritten reading only when the handwritten path captured content for a key
    that already exists in the custom-model output.
    """
    # 1) Base: every custom-model field (authoritative, complete).
    fields: dict[str, str] = {name: f.value for name, f in top_level.fields.items()}

    # 2) Overlay: fill only existing keys, only when the LLM captured something.
    for key, hw in handwritten.fields.items():
        if key not in fields:
            continue  # handwritten path never introduces new keys
        if hw.source.value == "none":
            continue  # nothing captured — keep the custom-model value
        if not hw.value:
            continue  # empty reading — keep the custom-model value
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
