"""Unit tests for tiled DI OCR: line parsing, coord remap, row banding (Req 5.2)."""

from __future__ import annotations

import contextvars
import io
import threading
import time

import pytest
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
    """DI client stub: returns a canned DIResult, or raises for a chosen call.

    Thread-safe: tiles are analyzed concurrently, so the call counter is locked
    (call order is not tile order under concurrency).
    """

    def __init__(self, fail_tile_index=None):
        self.calls = 0
        self.fail_tile_index = fail_tile_index
        self._lock = threading.Lock()

    def analyze(self, model_id, document, *, pages=None):
        with self._lock:
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


def test_ocr_tiles_all_tiles_failed_is_ocr_failed():
    # GIVEN every tile's OCR call fails
    from di_processor.failures import FailureCode, PipelineFailure

    class _AllFail:
        def analyze(self, *_a, **_k):
            raise RuntimeError("DI throttled")

    # WHEN tiling THEN OCR_FAILED, counting the tiles (option a)
    with pytest.raises(PipelineFailure) as info:
        ocr_tiles(
            _AllFail(), _tiles(3), page_number=1, page_width=800, page_height=1000
        )
    assert info.value.code is FailureCode.OCR_FAILED
    assert "failed_tiles=3" in info.value.safe_detail
    assert "tiles=3" in info.value.safe_detail


def test_ocr_tiles_no_tiles_is_not_a_failure():
    # GIVEN no tiles at all (nothing attempted) THEN not an OCR failure
    result = ocr_tiles(_FakeClient(), [], page_number=1, page_width=10, page_height=10)
    assert result["pages"][0]["rows"] == []


_TRACE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_TRACE", default=None
)


class _SlowWidthClient:
    """Answers each tile with text naming the tile's image width, after a delay
    that is longer for narrower (earlier) tiles -- so completions arrive out of
    submission order -- and records peak concurrency and the caller's context
    seen in each worker."""

    def __init__(self, delay=0.05):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.contexts = set()
        self._lock = threading.Lock()

    def analyze(self, model_id, document, *, pages=None):
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            self.contexts.add(_TRACE.get())
        width = Image.open(io.BytesIO(document)).width
        # later tiles finish first, so completion order != submission order
        time.sleep(self.delay * (1 + (100 - width) / 100))
        with self._lock:
            self.in_flight -= 1
        return DIResult(content="x", pages=[_page([(f"W{width}", 2, 2, 0.9)])])


def _distinct_tiles(n):
    # tile i has image width 50+i (identifies it) and sits in its own row band
    return [
        Tile(i + 1, 1, 1, "full", Image.new("RGB", (50 + i, 20), "white"), 0, i * 200)
        for i in range(n)
    ]


def test_ocr_tiles_parallel_keeps_each_result_on_its_own_tile():
    # GIVEN 12 tiles analyzed 4 at a time, completing out of order
    client = _SlowWidthClient()
    result = ocr_tiles(
        client,
        _distinct_tiles(12),
        page_number=1,
        page_width=800,
        page_height=3000,
        max_workers=4,
    )
    rows = result["pages"][0]["rows"]
    # THEN every tile's text is in that tile's row (results matched to tiles)
    assert [r["segments"][0]["text"] for r in rows] == [f"W{50 + i}" for i in range(12)]
    # AND calls really overlapped, never beyond the limit
    assert 1 < client.peak <= 4


def test_ocr_tiles_parallel_workers_keep_the_callers_context():
    # GIVEN a context value set by the caller (as document_id is for logging)
    client = _SlowWidthClient(delay=0.01)
    token = _TRACE.set("doc-123")
    try:
        ocr_tiles(
            client,
            _distinct_tiles(6),
            page_number=1,
            page_width=800,
            page_height=2000,
            max_workers=3,
        )
    finally:
        _TRACE.reset(token)
    # THEN every worker thread saw it
    assert client.contexts == {"doc-123"}


def test_ocr_tiles_concurrency_one_is_sequential():
    client = _SlowWidthClient(delay=0.01)
    ocr_tiles(
        client,
        _distinct_tiles(5),
        page_number=1,
        page_width=800,
        page_height=2000,
        max_workers=1,
    )
    assert client.peak == 1
