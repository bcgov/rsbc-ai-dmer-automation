"""Unit tests for page tiling geometry (Requirement 5.1)."""

from __future__ import annotations

from di_processor.extraction.splitter import (
    TILE_HEIGHT,
    create_tiles,
    make_width_segments,
)
from PIL import Image


def test_width_segments_full_mode():
    # GIVEN full mode THEN only the full-width segment is returned
    assert make_width_segments(1000, "full") == [(0, 1000)]


def test_width_segments_half_mode():
    # GIVEN half mode THEN full + left/right halves (overlapping) are returned
    segs = make_width_segments(1000, "half")
    assert segs[0] == (0, 1000)
    assert len(segs) == 3
    # left half starts at 0, right half ends at width
    assert segs[1][0] == 0
    assert segs[2][1] == 1000


def test_width_segments_quarter_mode_has_more_segments():
    assert len(make_width_segments(2000, "quarter")) > len(
        make_width_segments(2000, "half")
    )


def test_create_tiles_two_passes_and_offsets():
    # GIVEN a tall image
    img = Image.new("RGB", (1000, 800), "white")
    # WHEN tiled in half mode
    tiles = create_tiles(img, mode="half")
    # THEN tiles come from both vertical passes
    passes = {t.pass_num for t in tiles}
    assert passes == {1, 2}
    # first pass starts at y=0; second pass starts at half a tile height
    pass2_min_y = min(t.offset_y for t in tiles if t.pass_num == 2)
    assert pass2_min_y == TILE_HEIGHT // 2
    # every tile carries full-page offsets within the image bounds
    for t in tiles:
        assert 0 <= t.offset_x < img.width
        assert 0 <= t.offset_y < img.height
        assert t.image.size[0] > 0 and t.image.size[1] > 0


def test_create_tiles_numbered_sequentially():
    img = Image.new("RGB", (600, 400), "white")
    tiles = create_tiles(img)
    numbers = [t.tile_number for t in tiles]
    assert numbers == list(range(1, len(tiles) + 1))
