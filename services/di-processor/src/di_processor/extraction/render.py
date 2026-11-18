"""Page rendering for di-processor (ported from the POC).

Renders page 1 of a DMER PDF to a raster image via PyMuPDF and crops the top
"Protected B" section (personal identifiers / PII) before any downstream OCR or
LLM step. Scope is page 1 only.

Pure functions operating on in-memory bytes so they are unit-testable without
touching Blob Storage.
"""

from __future__ import annotations

import io

import fitz  # PyMuPDF
from PIL import Image

# Rendering / crop settings (from the POC).
DEFAULT_DPI = 300
CROP_TOP_PERCENT = 0.25
PAGE_NUMBER = 1  # page 1 only


def render_page(
    pdf_bytes: bytes, page_number: int = PAGE_NUMBER, dpi: int = DEFAULT_DPI
) -> Image.Image:
    """Render one PDF page to an RGB PIL image at ``dpi``.

    ``page_number`` is 1-based. Raises ``IndexError`` if the page is absent.
    """
    with fitz.open(stream=pdf_bytes, filetype="pdf") as pdf:
        if page_number < 1 or page_number > pdf.page_count:
            raise IndexError(
                f"page {page_number} out of range (document has {pdf.page_count} pages)"
            )
        page = pdf[page_number - 1]
        zoom = dpi / 72
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def crop_protected_b(
    image: Image.Image, crop_top_percent: float = CROP_TOP_PERCENT
) -> Image.Image:
    """Crop the top ``crop_top_percent`` of the page to remove Protected B PII.

    Returns the original image unchanged when ``crop_top_percent`` is 0.
    """
    if crop_top_percent <= 0:
        return image
    crop_y = int(image.height * crop_top_percent)
    return image.crop((0, crop_y, image.width, image.height))


def render_cropped_page(
    pdf_bytes: bytes,
    page_number: int = PAGE_NUMBER,
    dpi: int = DEFAULT_DPI,
    crop_top_percent: float = CROP_TOP_PERCENT,
) -> Image.Image:
    """Render page ``page_number`` and crop the Protected B section."""
    return crop_protected_b(render_page(pdf_bytes, page_number, dpi), crop_top_percent)


def image_to_png_bytes(image: Image.Image) -> bytes:
    """Encode a PIL image to PNG bytes (for sending to DI / the LLM)."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
