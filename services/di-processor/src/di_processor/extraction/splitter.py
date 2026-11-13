"""Page tiling for handwritten-section OCR (ported from the POC).

Splits a (cropped) page image into small overlapping tiles across two vertical
passes and configurable width segments. Tiling improves recall on faint
handwriting and keeps a row structure that ``di_ocr`` remaps back to full-page
coordinates.

Pure geometry + cropping; no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

# Tiling settings (from the POC).
TILE_HEIGHT = 175
OVERLAP = 40
SEGMENT_MODE = "half"  # "full" | "half" | "quarter"
_QUARTER_WIDTH = 500
_OVERLAP_X = 50
_SEGMENT_LABELS = ["full", "left_half", "right_half", "q1", "q2", "q3", "q4"]


@dataclass(frozen=True)
class Tile:
    """A single tile: cropped image plus its full-page pixel offsets."""

    tile_number: int
    row_id: int
    pass_num: int
    segment: str
    image: Image.Image
    offset_x: int
    offset_y: int


def make_width_segments(width: int, mode: str = SEGMENT_MODE) -> list[tuple[int, int]]:
    """Return (x_start, x_end) width segments for ``mode``.

    ``full`` -> whole width; ``half`` -> full + left/right halves; ``quarter``
    adds fixed-width quarter segments. Segments overlap by ``_OVERLAP_X`` px.
    """
    segments: list[tuple[int, int]] = [(0, width)]  # full width always

    if mode in ("half", "quarter"):
        mid = width // 2
        segments.append((0, min(mid + _OVERLAP_X, width)))
        segments.append((max(mid - _OVERLAP_X, 0), width))

    if mode == "quarter":
        x = 0
        while x < width:
            x_start = max(x - _OVERLAP_X, 0)
            x_end = min(x + _QUARTER_WIDTH + _OVERLAP_X, width)
            segments.append((x_start, x_end))
            if x_end >= width:
                break
            x += _QUARTER_WIDTH

    return segments


def create_tiles(
    image: Image.Image,
    *,
    tile_height: int = TILE_HEIGHT,
    overlap: int = OVERLAP,
    mode: str = SEGMENT_MODE,
) -> list[Tile]:
    """Split ``image`` into overlapping tiles across two vertical passes.

    The two passes are offset by half a tile height so a line straddling a tile
    boundary in one pass is captured whole in the other.
    """
    width, height = image.size
    tiles: list[Tile] = []
    tile_number = 1
    row_id = 1
    offsets = [0, tile_height // 2]

    for pass_num, start_y in enumerate(offsets, start=1):
        y = start_y
        while y < height:
            bottom = min(y + tile_height, height)
            if bottom - y < 30:
                break

            for seg_index, (x_start, x_end) in enumerate(
                make_width_segments(width, mode)
            ):
                crop = image.crop((x_start, y, x_end, bottom))
                seg_label = (
                    _SEGMENT_LABELS[seg_index]
                    if seg_index < len(_SEGMENT_LABELS)
                    else f"seg{seg_index}"
                )
                tiles.append(
                    Tile(
                        tile_number=tile_number,
                        row_id=row_id,
                        pass_num=pass_num,
                        segment=seg_label,
                        image=crop,
                        offset_x=x_start,
                        offset_y=y,
                    )
                )
                tile_number += 1

            row_id += 1
            if bottom >= height:
                break
            y = bottom - overlap

    return tiles
