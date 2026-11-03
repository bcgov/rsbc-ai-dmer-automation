"""Tests for blob path builders and BlobClient URL helpers (pure, no Azure I/O)."""

from __future__ import annotations

import pytest
from dmer_common.storage import (
    combined_path,
    containers,
    handwritten_path,
    ocr_path,
    top_level_path,
)


def test_path_builders_namespace_by_document_id():
    # GIVEN a document id
    # WHEN building each stage path
    # THEN the path is namespaced under the document id with the right filename
    assert top_level_path("doc-1") == "doc-1/top_level.json"
    assert ocr_path("doc-1") == "doc-1/ocr.json"
    assert handwritten_path("doc-1") == "doc-1/handwritten.json"
    assert combined_path("doc-1") == "doc-1/combined.json"


def test_path_builders_strip_stray_slashes():
    # GIVEN a document id with surrounding slashes/whitespace
    # WHEN building a path THEN they are normalized away
    assert ocr_path(" /doc-1/ ") == "doc-1/ocr.json"


@pytest.mark.parametrize("bad", ["", "   ", "/"])
def test_path_builders_reject_empty_document_id(bad):
    # GIVEN an empty document id
    # WHEN building a path THEN it raises
    with pytest.raises(ValueError):
        combined_path(bad)


def test_container_default(monkeypatch):
    # GIVEN no container env override
    monkeypatch.delenv("EXTRACTED_DMER_CONTAINER", raising=False)
    # THEN the getter returns the contracted default name (single container)
    assert containers.extracted_dmer() == "extracted-dmer"


def test_container_name_configurable_via_env(monkeypatch):
    # GIVEN an env override for the container name
    monkeypatch.setenv("EXTRACTED_DMER_CONTAINER", "extracted-custom")
    # THEN the getter reflects the override
    assert containers.extracted_dmer() == "extracted-custom"
