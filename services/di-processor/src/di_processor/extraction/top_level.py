"""Top-level field extraction via the custom-trained Document Intelligence model.

Stage A (new — not in the POC). Runs the custom-trained DI model over page 1 and
maps its ``documents[].fields`` output into :class:`TopLevelExtraction`. The
model id is configuration (the trained model's exact field set is owned outside
this repo), so mapping is permissive: any field the model emits is captured.

DI custom-model field values come back as dicts with a ``valueString`` /
``content`` and a ``confidence``; this normalizes them to ``TopLevelField``.
"""

from __future__ import annotations

from typing import Any

from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.telemetry import get_logger

from . import cutoff
from .schemas import TopLevelExtraction, TopLevelField

_log = get_logger(__name__)

# DI custom models analyze the whole document; scope to page 1 via the pages arg.
PAGE_1 = "1"


def _field_value(raw: dict[str, Any]) -> str:
    """Extract a string value from a DI field dict, preferring typed values.

    Handles the value types a DMER custom model emits, including checkbox
    (``selectionMark``) and boolean fields — the top-level checkboxes the model
    surfaces must not be dropped.
    """
    for key in (
        "valueString",
        "valueSelectionMark",  # "selected" / "unselected" (checkboxes)
        "valueBoolean",
        "valueDate",
        "valueNumber",
        "valueInteger",
        "valuePhoneNumber",
        "valueTime",
        "content",
    ):
        val = raw.get(key)
        if val is not None:
            return str(val)
    return ""


def map_di_fields(fields: dict[str, Any]) -> dict[str, TopLevelField]:
    """Map a DI ``documents[i]['fields']`` dict into TopLevelField entries."""
    out: dict[str, TopLevelField] = {}
    for name, raw in (fields or {}).items():
        if not isinstance(raw, dict):
            continue
        out[name] = TopLevelField(
            value=_field_value(raw), confidence=raw.get("confidence")
        )
    return out


def extract_top_level(
    client: DocumentIntelligenceClient, model_id: str, pdf_bytes: bytes
) -> TopLevelExtraction:
    """Run the custom model on page 1 and return the mapped top-level fields.

    Uses the first analyzed document's fields (DMER is a single-document form).
    Returns an empty extraction if the model yields no documents.
    """
    result = client.analyze(model_id, pdf_bytes, pages=PAGE_1)
    documents = result.documents or []
    fields = documents[0].get("fields", {}) if documents else {}
    mapped = map_di_fields(fields)
    # Cut-off check reuses this call's full-page layout (no extra DI call).
    flags = cutoff.detect_cutoff(result.pages[0] if result.pages else None, fields)
    _log.info(
        "custom-model top-level extraction complete",
        extra={
            "model_id": model_id,
            "field_count": len(mapped),
            "has_header": flags.has_header,
            "has_signature": flags.has_signature,
            "is_cutoff": flags.is_cutoff,
        },
    )
    return TopLevelExtraction(fields=mapped, cutoff=flags)
