"""Unit tests for page rendering and Protected B cropping (Requirement 5.1)."""

from __future__ import annotations

import fitz
import pytest
from di_processor.extraction.render import (
    CROP_TOP_PERCENT,
    crop_protected_b,
    image_to_png_bytes,
    render_cropped_page,
    render_page,
)
from PIL import Image


def _make_pdf(pages: int = 1) -> bytes:
    doc = fitz.open()
    for _ in range(pages):
        page = doc.new_page(width=612, height=792)  # US Letter points
        page.insert_text((72, 72), "DMER test page")
    data = doc.tobytes()
    doc.close()
    return data


def test_render_page_returns_rgb_image_at_dpi():
    # GIVEN a one-page PDF
    pdf = _make_pdf()
    # WHEN rendered at 300 DPI
    img = render_page(pdf, page_number=1, dpi=300)
    # THEN it is an RGB image scaled by 300/72 (612pt -> 2550px wide)
    assert img.mode == "RGB"
    assert img.width == pytest.approx(2550, abs=2)


def test_render_page_out_of_range_raises():
    # GIVEN a one-page PDF
    pdf = _make_pdf(pages=1)
    # WHEN page 2 is requested THEN it raises (page-1 scope guard)
    with pytest.raises(IndexError):
        render_page(pdf, page_number=2)


def test_crop_protected_b_removes_top_fraction():
    # GIVEN a 1000px-tall image
    img = Image.new("RGB", (800, 1000), "white")
    # WHEN cropping the default top percent
    cropped = crop_protected_b(img)
    # THEN height is reduced by CROP_TOP_PERCENT and width is unchanged
    assert cropped.height == 1000 - int(1000 * CROP_TOP_PERCENT)
    assert cropped.width == 800


def test_crop_protected_b_zero_percent_is_noop():
    img = Image.new("RGB", (10, 10), "white")
    assert crop_protected_b(img, 0).size == (10, 10)


def test_render_cropped_page_combines_render_and_crop():
    pdf = _make_pdf()
    full = render_page(pdf, dpi=150)
    cropped = render_cropped_page(pdf, dpi=150)
    # cropped height is shorter than the full render
    assert cropped.height < full.height


def test_image_to_png_bytes_roundtrips():
    img = Image.new("RGB", (20, 20), "white")
    data = image_to_png_bytes(img)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(__import__("io").BytesIO(data)).size == (20, 20)
