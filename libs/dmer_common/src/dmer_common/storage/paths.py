"""Pure blob path builders for the di-processor extraction artifacts.

Kept free of any Azure SDK so they can be unit-tested in isolation. Each builder
returns the blob path (the name within a container), namespaced by document id.

Under the revised architecture all four artifacts live under the single
``extracted-dmer`` container (see ``docs/development/stages/02-extraction.md``),
kept as separate named files per document:

    extracted-dmer/<document_id>/top_level.json
    extracted-dmer/<document_id>/ocr.json
    extracted-dmer/<document_id>/handwritten.json
    extracted-dmer/<document_id>/combined.json
"""

from __future__ import annotations


def _doc(document_id: str) -> str:
    document_id = (document_id or "").strip().strip("/")
    if not document_id:
        raise ValueError("document_id must be a non-empty string")
    return document_id


def top_level_path(document_id: str) -> str:
    """Stage A: custom-model top-level fields JSON."""
    return f"{_doc(document_id)}/top_level.json"


def ocr_path(document_id: str) -> str:
    """Stage B: tiled ``prebuilt-read`` OCR JSON."""
    return f"{_doc(document_id)}/ocr.json"


def handwritten_path(document_id: str) -> str:
    """Stage C: LLM-reconstructed handwritten fields JSON."""
    return f"{_doc(document_id)}/handwritten.json"


def combined_path(document_id: str) -> str:
    """Stage D: unified combined extraction JSON."""
    return f"{_doc(document_id)}/combined.json"
