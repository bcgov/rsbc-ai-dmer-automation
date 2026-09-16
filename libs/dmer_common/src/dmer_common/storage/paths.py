"""Pure blob path builders for the di-processor extraction artifacts.

Kept free of any Azure SDK so they can be unit-tested in isolation. Each builder
returns the blob path (the name within a container), namespaced by document id.
See the blob layout in ``.kiro/specs/di-processor/design.md``.
"""

from __future__ import annotations


def _doc(document_id: str) -> str:
    document_id = (document_id or "").strip().strip("/")
    if not document_id:
        raise ValueError("document_id must be a non-empty string")
    return document_id


def top_level_path(document_id: str) -> str:
    """Stage A: custom-model top-level fields JSON (in ``extracted-dmer``)."""
    return f"{_doc(document_id)}/top_level.json"


def ocr_path(document_id: str) -> str:
    """Stage B: tiled ``prebuilt-read`` OCR JSON (in ``extracted-dmer``)."""
    return f"{_doc(document_id)}/ocr.json"


def handwritten_path(document_id: str) -> str:
    """Stage C: LLM-reconstructed handwritten fields JSON (in ``extracted-dmer``)."""
    return f"{_doc(document_id)}/handwritten.json"


def combined_path(document_id: str) -> str:
    """Stage D: unified combined extraction JSON (in ``combined-extracted-dmer``)."""
    return f"{_doc(document_id)}/combined.json"
