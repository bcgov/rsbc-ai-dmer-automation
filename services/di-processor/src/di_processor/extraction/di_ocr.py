"""Tiled Document Intelligence OCR for the handwritten path (ported from the POC).

Runs the ``prebuilt-read`` model over each tile via
``dmer_common.doc_intelligence`` (Managed Identity, private endpoint, retry +
breaker), then remaps each tile's line coordinates back to full-page pixels and
groups them into vertical "row bands" — producing the ``rows[].segments[]`` JSON
shape the LLM reconstruction step consumes.

The DI client returns a normalized :class:`DIResult` whose ``pages`` are plain
dicts, so line/word parsing here operates on dicts (not raw SDK objects).
"""

from __future__ import annotations

import logging
from typing import Any

from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.telemetry import get_logger

from ..failures import FailureCode, PipelineFailure
from .render import image_to_png_bytes
from .splitter import TILE_HEIGHT, Tile

_log = get_logger(__name__)

PREBUILT_READ = "prebuilt-read"
# Row grouping band size (px) — half a tile height so both vertical passes land
# in the same band (matches the POC's group_results_by_row).
BAND_SIZE = max(TILE_HEIGHT // 2, 1)


def _poly_top_left(polygon: list[float]) -> tuple[float, float]:
    """Return (x_left, y_top) from a DI polygon (flat [x0,y0,x1,y1,...])."""
    if len(polygon) < 2:
        return 0.0, 0.0
    xs = polygon[0::2]
    ys = polygon[1::2]
    return min(xs), min(ys)


def _line_confidence(line: dict[str, Any], words: list[dict[str, Any]]) -> float | None:
    """Average the confidence of words overlapping a line's character spans."""
    spans = line.get("spans") or []
    if not spans or not words:
        return None
    ranges = [(s["offset"], s["offset"] + s["length"]) for s in spans]
    confs: list[float] = []
    for w in words:
        span = w.get("span")
        conf = w.get("confidence")
        if span is None or conf is None:
            continue
        start = span["offset"]
        if any(lo <= start < hi for lo, hi in ranges):
            confs.append(conf)
    return sum(confs) / len(confs) if confs else None


def di_lines(
    result_pages: list[dict[str, Any]],
) -> list[tuple[str, float, float, float | None]]:
    """Yield (text, x_left, y_top, confidence) for each line across pages."""
    out: list[tuple[str, float, float, float | None]] = []
    for page in result_pages:
        words = page.get("words") or []
        for line in page.get("lines") or []:
            x_left, y_top = _poly_top_left(line.get("polygon") or [])
            out.append(
                (line.get("content", ""), x_left, y_top, _line_confidence(line, words))
            )
    return out


def tiles_to_rows(
    tile_results: list[dict[str, Any]], band_size: int = BAND_SIZE
) -> list[dict[str, Any]]:
    """Group per-tile DI lines by vertical band into the rows/segments shape.

    Each entry in ``tile_results`` is ``{tile_number, pass_num, segment,
    offset_x, offset_y, pages}`` where ``pages`` is the tile's DIResult pages.
    Tile-relative line coordinates are remapped to full-page pixels before binning.
    """
    rows: dict[int, dict[str, Any]] = {}

    for tr in tile_results:
        offset_x = tr["offset_x"]
        offset_y = tr["offset_y"]
        lines = di_lines(tr.get("pages") or [])

        texts = [(offset_x + x_rel, text) for text, x_rel, _y_rel, _c in lines]
        confs = [c for _t, _x, _y, c in lines if c is not None]
        row_lines = sorted(texts, key=lambda t: t[0])

        segment_entry = {
            "tile_number": tr["tile_number"],
            "pass_num": tr["pass_num"],
            "segment": tr["segment"],
            "offset_x": offset_x,
            "offset_y": offset_y,
            "text": "\n".join(t[1] for t in row_lines),
            "block_count": len(row_lines),
            "scores": [round(c, 3) for c in confs],
            "avg_score": round(sum(confs) / len(confs), 3) if confs else 0.0,
        }

        band_key = offset_y // band_size
        row = rows.setdefault(
            band_key,
            {"band_key": band_key, "y_start": band_key * band_size, "segments": []},
        )
        row["segments"].append(segment_entry)

    ordered = sorted(rows.values(), key=lambda r: r["y_start"])
    for i, row in enumerate(ordered, start=1):
        row["row_id"] = i
    return ordered


def ocr_tiles(
    client: DocumentIntelligenceClient,
    tiles: list[Tile],
    page_number: int,
    page_width: int,
    page_height: int,
) -> dict[str, Any]:
    """OCR every tile with ``prebuilt-read`` and assemble the rows/segments JSON.

    A tile that fails to analyze is skipped (its lines are simply absent), so a
    single bad tile does not abort the whole page. If **every** tile fails, the
    page has no OCR at all and raises ``OCR_FAILED`` rather than letting the LLM
    reconstruct handwriting from the image alone.
    """
    tile_results: list[dict[str, Any]] = []
    failed = 0
    for tile in tiles:
        try:
            di_result = client.analyze(PREBUILT_READ, image_to_png_bytes(tile.image))
            pages = di_result.pages
        except Exception:  # noqa: BLE001 - one bad tile must not abort the page
            _log.warning(
                "tile OCR failed; skipping",
                extra={"tile_number": tile.tile_number, "segment": tile.segment},
            )
            pages = []
            failed += 1
        tile_results.append(
            {
                "tile_number": tile.tile_number,
                "pass_num": tile.pass_num,
                "segment": tile.segment,
                "offset_x": tile.offset_x,
                "offset_y": tile.offset_y,
                "pages": pages,
            }
        )

    if tiles and failed == len(tiles):
        raise PipelineFailure(
            FailureCode.OCR_FAILED,
            f"error=AllTilesFailed; failed_tiles={failed}; tiles={len(tiles)}",
        )

    rows = tiles_to_rows(tile_results)
    _log.log(
        logging.WARNING if failed else logging.INFO,
        "tiled OCR complete",
        extra={"tiles": len(tiles), "failed_tiles": failed, "rows": len(rows)},
    )
    return {
        "ocr_engine": "azure_document_intelligence",
        "total_pages": 1,
        "pages": [
            {
                "page_number": page_number,
                "page_width": page_width,
                "page_height": page_height,
                "processing_method": "document_intelligence_read_tiled",
                "rows": rows,
            }
        ],
    }
