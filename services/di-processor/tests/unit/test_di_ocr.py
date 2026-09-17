"""Unit tests for tiled DI OCR: line parsing, coord remap, row banding (Req 5.2)."""

from __future__ import annotations

from di_processor.extraction.di_ocr import di_lines, ocr_tiles, tiles_to_rows
from di_processor.extraction.splitter import Tile
from dmer_common.doc_intelligence import DIResult
from PIL import Image


def _page(lines):
    """Build a DI page dict with the given (content, x, y, conf) lines."""
    page_lines = []
    words = []
    offset = 0
    for i, (text, x, y, conf) in enumerate(lines):
        length = len(text)
        page_lines.append(
            {
                "content": text,
                "polygon": [x, y, x + 10, y, x + 10, y + 5, x, y + 5],
                "spans": [{"offset": offset, "length": length}],
            }
        )
        if conf is not None:
            words.append(
                {"span": {"offset": offset, "length": length}, "confidence": conf}
            )
        offset += length + 1
    return {"lines": page_lines, "words": words}


def test_di_lines_extracts_text_and_topleft():
    pages = [_page([("hello", 30, 12, 0.9)])]
    lines = di_lines(pages)
    assert lines == [("hello", 30, 12, 0.9)]


def test_di_lines_confidence_none_when_no_words():
    pages = [_page([("x", 5, 5, None)])]
    _, _, _, conf = di_lines(pages)[0]
    assert conf is None


def test_tiles_to_rows_remaps_offsets_and_bands():
    # two tiles at different y offsets → two row bands; x is remapped by offset_x
    tile_results = [
        {
            "tile_number": 1,
            "pass_num": 1,
            "segment": "full",
            "offset_x": 100,
            "offset_y": 0,
            "pages": [_page([("A", 5, 2, 0.8)])],
        },
        {
            "tile_number": 2,
            "pass_num": 1,
            "segment": "full",
            "offset_x": 0,
            "offset_y": 500,
            "pages": [_page([("B", 3, 2, 0.6)])],
        },
    ]
    rows = tiles_to_rows(tile_results, band_size=100)
    assert len(rows) == 2
    # rows are ordered by y_start and re-numbered from 1
    assert [r["row_id"] for r in rows] == [1, 2]
    assert rows[0]["segments"][0]["text"] == "A"
    # avg_score computed from word confidences
    assert rows[0]["segments"][0]["avg_score"] == 0.8


def test_tiles_in_same_band_grouped_together():
    tile_results = [
        {
            "tile_number": 1,
            "pass_num": 1,
            "segment": "full",
            "offset_x": 0,
            "offset_y": 0,
            "pages": [_page([("A", 1, 1, None)])],
        },
        {
            "tile_number": 2,
            "pass_num": 2,
            "segment": "full",
            "offset_x": 0,
            "offset_y": 40,
            "pages": [_page([("B", 1, 1, None)])],
        },
    ]
    rows = tiles_to_rows(tile_results, band_size=100)
    assert len(rows) == 1
    assert len(rows[0]["segments"]) == 2


class _FakeClient:
    """DI client stub: returns a canned DIResult, or raises for a chosen tile."""

    def __init__(self, fail_tile_index=None):
        self.calls = 0
        self.fail_tile_index = fail_tile_index

    def analyze(self, model_id, document, *, pages=None):
        idx = self.calls
        self.calls += 1
        if idx == self.fail_tile_index:
            raise RuntimeError("DI throttled")
        return DIResult(content="x", pages=[_page([(f"L{idx}", 2, 2, 0.7)])])


def _tiles(n):
    img = Image.new("RGB", (50, 20), "white")
    return [Tile(i + 1, 1, 1, "full", img, 0, i * 200) for i in range(n)]


def test_ocr_tiles_assembles_page_shape():
    client = _FakeClient()
    result = ocr_tiles(
        client, _tiles(2), page_number=1, page_width=800, page_height=1000
    )
    assert result["pages"][0]["page_number"] == 1
    assert result["pages"][0]["processing_method"] == "document_intelligence_read_tiled"
    assert len(result["pages"][0]["rows"]) == 2


def test_ocr_tiles_skips_failed_tile():
    # GIVEN the 2nd tile fails
    client = _FakeClient(fail_tile_index=1)
    result = ocr_tiles(
        client, _tiles(3), page_number=1, page_width=800, page_height=1000
    )
    # THEN OCR still completes; the failed tile contributes an empty segment
    all_text = [
        seg["text"] for row in result["pages"][0]["rows"] for seg in row["segments"]
    ]
    assert "" in all_text  # failed tile produced no lines
    assert client.calls == 3
