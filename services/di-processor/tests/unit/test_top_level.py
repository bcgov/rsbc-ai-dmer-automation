"""Unit tests for custom-model top-level extraction (Req 4.1-4.4)."""

from __future__ import annotations

from di_processor.extraction.schemas import TopLevelExtraction
from di_processor.extraction.top_level import extract_top_level, map_di_fields
from dmer_common.doc_intelligence import DIResult


def test_map_di_fields_prefers_typed_values():
    fields = {
        "physician.name": {"valueString": "Dr X", "confidence": 0.98},
        "exam.date": {"valueDate": "2026-01-02", "confidence": 0.9},
        "raw.only": {"content": "scribble", "confidence": 0.4},
        "not.a.dict": "ignored",
    }
    mapped = map_di_fields(fields)
    assert mapped["physician.name"].value == "Dr X"
    assert mapped["physician.name"].confidence == 0.98
    assert mapped["exam.date"].value == "2026-01-02"
    assert mapped["raw.only"].value == "scribble"
    assert "not.a.dict" not in mapped


def test_map_di_fields_captures_checkbox_and_boolean():
    # GIVEN checkbox (selectionMark) and boolean fields from the custom model
    fields = {
        "vision.corrective_lenses": {
            "valueSelectionMark": "selected",
            "confidence": 0.99,
        },
        "has_concerns": {"valueBoolean": True, "confidence": 0.95},
    }
    mapped = map_di_fields(fields)
    # THEN they are captured, not dropped
    assert mapped["vision.corrective_lenses"].value == "selected"
    assert mapped["has_concerns"].value == "True"


class _FakeClient:
    def __init__(self, result):
        self._result = result
        self.last_call = None

    def analyze(self, model_id, document, *, pages=None):
        self.last_call = (model_id, pages)
        return self._result


def test_extract_top_level_maps_first_document():
    result = DIResult(
        content="",
        documents=[
            {"fields": {"physician.name": {"valueString": "Dr Y", "confidence": 0.95}}}
        ],
    )
    client = _FakeClient(result)
    extraction = extract_top_level(client, "dmer-custom-v1", b"%PDF-fake")
    assert isinstance(extraction, TopLevelExtraction)
    assert extraction.fields["physician.name"].value == "Dr Y"
    # custom model invoked on page 1 with the configured model id
    assert client.last_call == ("dmer-custom-v1", "1")


def test_extract_top_level_no_documents_returns_empty():
    client = _FakeClient(DIResult(content="", documents=[]))
    extraction = extract_top_level(client, "dmer-custom-v1", b"%PDF-fake")
    assert extraction.fields == {}
