import os
import io
import json
import time
import logging
import tempfile

import fitz  # PyMuPDF
import requests
from PIL import Image

# ============================================================
# CONFIGURATION
# ============================================================

PDF_FILE = "DMER23_.pdf"

# PaddleOCR API on Azure Container Apps (GPU)
OCR_API_URL = "https://paddleocr-gpu-app.orangewater-9804500d.canadacentral.azurecontainerapps.io/ocr"
OCR_HEALTH_URL = "https://paddleocr-gpu-app.orangewater-9804500d.canadacentral.azurecontainerapps.io/"

# API key sent as the X-API-Key header. Prefer the OCR_API_KEY env var; the value
# below is a fallback for quick testing. Do NOT commit real keys to source control.
OCR_API_KEY = os.getenv("OCR_API_KEY", "pN5jSmS4BaiWJEZJwLAK8eVhisIkv0GzTpuGLz3OmTg")


def _ocr_headers():
    """Build request headers, including the API key if one is configured."""
    if OCR_API_KEY:
        return {"X-API-Key": OCR_API_KEY}
    return {}

# PDF rendering resolution
DPI = 300

# Split pages larger than these dimensions
MAX_WIDTH = 800
MAX_HEIGHT = 800

# Horizontal tile settings
TILE_HEIGHT = 175
OVERLAP = 40

# Max tile dimensions to send to OCR API (resize before sending)
# Split wide tiles in half and send each half separately
OCR_MAX_WIDTH = 1200
OCR_MAX_HEIGHT = 400

# Split tiles wider than this into left/right halves
TILE_SPLIT_WIDTH = 1000

# Which width segments to run per row:
#   "full"      -> full width only (fastest, for quick checks)
#   "half"      -> full + left/right halves
#   "quarter"   -> full + halves + quarter-width segments
SEGMENT_MODE = "half"

# Testing: only process rows in this range (1-based, inclusive).
# Set TEST_START_ROW = None to process all rows.
TEST_START_ROW = None
TEST_END_ROW = None  # None = process to the end

# Only process pages up to this number (1-based). Set to None for all pages.
MAX_PAGES = 1

# Output JSON - derived automatically from PDF_FILE
# e.g. "DMER23_.pdf" -> "DMER23_pdf_1.json"
OUTPUT_FILE = os.path.splitext(PDF_FILE)[0].rstrip("_") + "_pdf_1.json"

# OCR API timeout in seconds
REQUEST_TIMEOUT = 900

# Max retries per tile
MAX_RETRIES = 2

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

logger = logging.getLogger(__name__)


# ============================================================
# PDF TO IMAGES
# ============================================================

def pdf_to_images(pdf_path):
    logger.info("=" * 70)
    logger.info("OPENING PDF: %s", pdf_path)
    logger.info("RENDER DPI: %s", DPI)
    logger.info("=" * 70)

    start_time = time.time()

    pdf = fitz.open(pdf_path)

    total_pages = len(pdf)
    if MAX_PAGES is not None:
        total_pages = min(total_pages, MAX_PAGES)

    logger.info("PAGES TO PROCESS: %s", total_pages)

    zoom = DPI / 72
    matrix = fitz.Matrix(zoom, zoom)

    pages = []

    for page_number, page in enumerate(pdf, start=1):

        # Stop after MAX_PAGES (e.g., process only page 1)
        if MAX_PAGES is not None and page_number > MAX_PAGES:
            logger.info("Reached MAX_PAGES (%s), stopping render.", MAX_PAGES)
            break

        page_start = time.time()

        logger.info(
            "[PAGE %s/%s] Rendering page...",
            page_number,
            total_pages
        )

        pix = page.get_pixmap(
            matrix=matrix,
            alpha=False
        )

        image = Image.open(
            io.BytesIO(pix.tobytes("png"))
        ).convert("RGB")

        width, height = image.size

        elapsed = time.time() - page_start

        logger.info(
            "[PAGE %s/%s] Rendered: %s x %s pixels | %.2f seconds",
            page_number,
            total_pages,
            width,
            height,
            elapsed
        )

        pages.append({
            "page_number": page_number,
            "image": image
        })

    pdf.close()

    logger.info(
        "PDF rendering completed in %.2f seconds",
        time.time() - start_time
    )

    return pages


# ============================================================
# CHECK IF PAGE SHOULD BE SPLIT
# ============================================================

def should_split(image):
    width, height = image.size

    split = (
        width > MAX_WIDTH
        or height > MAX_HEIGHT
    )

    logger.info(
        "SIZE CHECK | %s x %s | Max: %s x %s | Split: %s",
        width,
        height,
        MAX_WIDTH,
        MAX_HEIGHT,
        split
    )

    return split


# ============================================================
# CREATE OVERLAPPING HORIZONTAL TILES
# ============================================================

def make_width_segments(width):
    """Return list of (x_start, x_end) segments for full, half, and quarter widths.
    Each segment includes a small horizontal overlap to avoid cutting text."""
    overlap_x = 50
    segments = []

    # Full width (always included)
    segments.append((0, width))

    # Half widths (left, right) - included for "half" and "quarter" modes
    if SEGMENT_MODE in ("half", "quarter"):
        mid = width // 2
        segments.append((0, min(mid + overlap_x, width)))
        segments.append((max(mid - overlap_x, 0), width))

    # Quarter-width segments (500 px each) - included for "quarter" mode only
    if SEGMENT_MODE == "quarter":
        quarter_width = 500
        x = 0
        while x < width:
            x_start = max(x - overlap_x, 0)
            x_end = min(x + quarter_width + overlap_x, width)
            segments.append((x_start, x_end))
            if x_end >= width:
                break
            x += quarter_width

    return segments


def create_tiles(image):
    width, height = image.size

    logger.info(
        "Creating tiles | Image: %sx%s | Tile height: %s | Overlap: %s",
        width,
        height,
        TILE_HEIGHT,
        OVERLAP
    )

    tiles = []
    tile_number = 1
    row_id = 1

    # Band sizing must match group_results_by_row so filtering aligns with output rows
    band_size = max(TILE_HEIGHT // 2, 1)

    # Two vertical passes with different Y offsets to catch content at boundaries
    offsets = [0, TILE_HEIGHT // 2]

    segment_labels = ["full", "left_half", "right_half", "q1", "q2", "q3", "q4"]

    for pass_num, start_y in enumerate(offsets, start=1):
        y = start_y

        while y < height:
            bottom = min(y + TILE_HEIGHT, height)

            # Skip very small remaining strips
            if bottom - y < 30:
                break

            # Test mode: skip Y-bands outside the configured row range.
            # Band index is 1-based to match output row_id ordering.
            band_index = (y // band_size) + 1
            if TEST_START_ROW is not None and band_index < TEST_START_ROW:
                y = bottom - OVERLAP
                continue
            if TEST_END_ROW is not None and band_index > TEST_END_ROW:
                break

            # Each row = one Y-band. All width segments belong to this row.
            segments = make_width_segments(width)

            for seg_index, (x_start, x_end) in enumerate(segments):
                crop = image.crop((x_start, y, x_end, bottom))
                seg_label = (
                    segment_labels[seg_index]
                    if seg_index < len(segment_labels)
                    else f"seg{seg_index}"
                )

                logger.info(
                    "Pass %s | Row %s | Tile %s (%s) | x=%s-%s | y=%s-%s | Size=%sx%s",
                    pass_num, row_id, tile_number, seg_label,
                    x_start, x_end, y, bottom,
                    crop.size[0], crop.size[1]
                )
                tiles.append({
                    "tile_number": tile_number,
                    "row_id": row_id,
                    "pass_num": pass_num,
                    "segment": seg_label,
                    "image": crop,
                    "offset_x": x_start,
                    "offset_y": y
                })
                tile_number += 1

            row_id += 1

            if bottom >= height:
                break

            y = bottom - OVERLAP

    logger.info(
        "Total tiles created: %s across %s rows (2 passes x 7 width segments)",
        len(tiles), row_id - 1
    )

    return tiles


# ============================================================
# CALL EXISTING PADDLEOCR API
# ============================================================

def call_ocr_api(image, page_number, tile_number, total_tiles):

    temp_path = None

    try:
        logger.info(
            "[PAGE %s | TILE %s/%s] ENTERED call_ocr_api()",
            page_number,
            tile_number,
            total_tiles
        )

        # Scale down tile to fit OCR memory limits
        image, scale_factor = scale_down_if_needed(image)

        # Save tile to debug folder for inspection
        debug_dir = "tiles_debug"
        os.makedirs(debug_dir, exist_ok=True)
        tile_filename = f"page_{page_number}_tile_{tile_number}.png"
        temp_path = os.path.join(debug_dir, tile_filename)

        logger.info(
            "[PAGE %s | TILE %s/%s] Saving tile image: %s",
            page_number,
            tile_number,
            total_tiles,
            temp_path
        )

        image.save(temp_path, "PNG")

        logger.info(
            "[PAGE %s | TILE %s/%s] Temporary image saved successfully",
            page_number,
            tile_number,
            total_tiles
        )

        logger.info(
            "[PAGE %s | TILE %s/%s] File size: %.2f MB",
            page_number,
            tile_number,
            total_tiles,
            os.path.getsize(temp_path) / (1024 * 1024)
        )

        logger.info(
            "[PAGE %s | TILE %s/%s] ABOUT TO POST TO: %s",
            page_number,
            tile_number,
            total_tiles,
            OCR_API_URL
        )

        start_time = time.time()

        with open(temp_path, "rb") as file:

            response = requests.post(
                OCR_API_URL,
                files={
                    "file": (
                        f"page_{page_number}_tile_{tile_number}.png",
                        file,
                        "image/png"
                    )
                },
                headers=_ocr_headers(),
                timeout=REQUEST_TIMEOUT
            )

        elapsed = time.time() - start_time

        logger.info(
            "[PAGE %s | TILE %s/%s] POST COMPLETED",
            page_number,
            tile_number,
            total_tiles
        )

        logger.info(
            "[PAGE %s | TILE %s/%s] Status code: %s",
            page_number,
            tile_number,
            total_tiles,
            response.status_code
        )

        logger.info(
            "[PAGE %s | TILE %s/%s] Request time: %.2f seconds",
            page_number,
            tile_number,
            total_tiles,
            elapsed
        )

        if response.status_code != 200:

            logger.error(
                "OCR API ERROR RESPONSE: %s",
                response.text
            )

        response.raise_for_status()

        logger.info(
            "[PAGE %s | TILE %s/%s] Parsing JSON response",
            page_number,
            tile_number,
            total_tiles
        )

        result = response.json()

        logger.info(
            "[PAGE %s | TILE %s/%s] API Response:\n%s",
            page_number,
            tile_number,
            total_tiles,
            json.dumps(result, indent=2, ensure_ascii=False)
        )

        return result, scale_factor

    except Exception as e:

        logger.exception(
            "[PAGE %s | TILE %s/%s] ERROR IN call_ocr_api: %s",
            page_number,
            tile_number,
            total_tiles,
            str(e)
        )

        raise


# ============================================================
# PROCESS ONE PDF PAGE
# ============================================================

def scale_down_if_needed(image):
    """Scale image down so it fits within OCR_MAX_WIDTH x OCR_MAX_HEIGHT, preserving aspect ratio.
    Returns (scaled_image, scale_factor)."""
    width, height = image.size

    if width <= OCR_MAX_WIDTH and height <= OCR_MAX_HEIGHT:
        return image, 1.0

    scale = min(OCR_MAX_WIDTH / width, OCR_MAX_HEIGHT / height)
    new_width = int(width * scale)
    new_height = int(height * scale)

    logger.info(
        "Scaling tile for OCR: %sx%s -> %sx%s (scale=%.2f)",
        width, height, new_width, new_height, scale
    )

    return image.resize((new_width, new_height), Image.LANCZOS), scale


# ============================================================
# GROUP RESULTS BY ROW
# ============================================================

def group_results_by_row(tile_results):
    """Group all tile OCR outputs by vertical band (actual Y position).

    Tiles from different passes that cover the same vertical area are grouped
    into the same row. Every segment's result is preserved - nothing dropped.
    """
    # Bin tiles into vertical bands. Band size = half a tile height so that
    # both passes (offset by TILE_HEIGHT//2) land in the same band.
    band_size = max(TILE_HEIGHT // 2, 1)

    rows = {}

    for tile_data in tile_results:
        offset_y = tile_data.get("offset_y") or 0

        # Assign a band key based on Y position
        band_key = offset_y // band_size

        result = tile_data["result"]
        pages = result.get("pages", [])
        parsing_list = []

        if pages:
            ocr_data = pages[0].get("result", {})
            if "res" in ocr_data:
                ocr_data = ocr_data["res"]
            parsing_list = ocr_data.get("parsing_res_list", [])

            # Extract layout detection scores keyed by block bbox
            layout_scores = {}
            layout_det = ocr_data.get("layout_det_res", {})
            for box in layout_det.get("boxes", []):
                coord = tuple(box.get("coordinate", []))
                layout_scores[coord] = round(box.get("score", 0.0), 3)

        texts = []
        block_scores = []
        for b in parsing_list:
            content = b.get("block_content", "").strip()
            if content:
                texts.append(content)
                bbox_key = tuple(b.get("block_bbox", []))
                score = layout_scores.get(bbox_key, None)
                block_scores.append(score)

        segment_entry = {
            "tile_number": tile_data.get("tile_number"),
            "pass_num": tile_data.get("pass_num"),
            "segment": tile_data.get("segment"),
            "offset_x": tile_data.get("offset_x"),
            "offset_y": offset_y,
            "text": "\n".join(texts),
            "block_count": len(texts),
            "scores": block_scores,
            "avg_score": round(sum(s for s in block_scores if s is not None) / max(len([s for s in block_scores if s is not None]), 1), 3),
        }

        if band_key not in rows:
            rows[band_key] = {
                "band_key": band_key,
                "y_start": band_key * band_size,
                "segments": [],
            }

        rows[band_key]["segments"].append(segment_entry)

    # Sort rows by vertical position and assign sequential row_id
    ordered = sorted(rows.values(), key=lambda r: r["y_start"])
    for i, row in enumerate(ordered, start=1):
        row["row_id"] = i

    return ordered


def process_page(page_number, image, total_pages):
    page_start = time.time()

    width, height = image.size

    logger.info("")
    logger.info("=" * 70)
    logger.info(
        "STARTING PAGE %s/%s",
        page_number,
        total_pages
    )
    logger.info(
        "Page size: %s x %s",
        width,
        height
    )
    logger.info("=" * 70)

    if should_split(image):

        logger.info(
            "Large page detected. Splitting into horizontal overlapping tiles."
        )

        tiles = create_tiles(image)

        processing_method = "horizontal_tiles"

    else:

        logger.info(
            "Page is small enough. Processing full page."
        )

        tiles = [{
            "tile_number": 1,
            "image": image,
            "offset_x": 0,
            "offset_y": 0
        }]

        processing_method = "full_page"

    total_tiles = len(tiles)

    logger.info(
        "PAGE %s | Method: %s | Tiles: %s",
        page_number,
        processing_method,
        total_tiles
    )

    tile_results = []

    for tile in tiles:

        tile_number = tile["tile_number"]

        logger.info(
            "PROGRESS | PAGE %s/%s | TILE %s/%s",
            page_number,
            total_pages,
            tile_number,
            total_tiles
        )

        try:
            result, scale_factor = call_ocr_api(
                image=tile["image"],
                page_number=page_number,
                tile_number=tile_number,
                total_tiles=total_tiles
            )

            tile_results.append({
                "tile_number": tile_number,
                "row_id": tile.get("row_id"),
                "pass_num": tile.get("pass_num"),
                "segment": tile.get("segment"),
                "offset_x": tile["offset_x"],
                "offset_y": tile["offset_y"],
                "scale_factor": scale_factor,
                "result": result
            })

        except Exception as e:
            logger.error(
                "TILE %s/%s FAILED, skipping: %s",
                tile_number,
                total_tiles,
                str(e)
            )
            tile_results.append({
                "tile_number": tile_number,
                "row_id": tile.get("row_id"),
                "pass_num": tile.get("pass_num"),
                "segment": tile.get("segment"),
                "offset_x": tile["offset_x"],
                "offset_y": tile["offset_y"],
                "scale_factor": 1.0,
                "result": {"pages": [], "error": str(e)}
            })

    elapsed = time.time() - page_start

    logger.info(
        "PAGE %s COMPLETED | %.2f seconds",
        page_number,
        elapsed
    )

    # Group all results by row (every segment preserved, nothing dropped)
    rows = group_results_by_row(tile_results)

    logger.info(
        "PAGE %s GROUPED | %s rows",
        page_number,
        len(rows)
    )

    return {
        "page_number": page_number,
        "page_width": width,
        "page_height": height,
        "processing_method": processing_method,
        "tile_count": total_tiles,
        "processing_time_seconds": round(elapsed, 2),
        "rows": rows,
    }


# ============================================================
# PROCESS ENTIRE PDF
# ============================================================

def process_pdf(pdf_path):
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(
            f"PDF not found: {pdf_path}"
        )

    total_start = time.time()

    pages = pdf_to_images(pdf_path)

    total_pages = len(pages)

    logger.info("")
    logger.info("=" * 70)
    logger.info("STARTING OCR PROCESSING")
    logger.info("TOTAL PAGES: %s", total_pages)
    logger.info("=" * 70)

    final_result = {
        "source_file": pdf_path,
        "dpi": DPI,
        "total_pages": total_pages,
        "pages": []
    }

    for page_data in pages:

        page_result = process_page(
            page_number=page_data["page_number"],
            image=page_data["image"],
            total_pages=total_pages
        )

        final_result["pages"].append(
            page_result
        )

        logger.info(
            "OVERALL PROGRESS: %s/%s pages completed",
            page_data["page_number"],
            total_pages
        )

    total_elapsed = time.time() - total_start

    final_result["total_processing_time_seconds"] = round(total_elapsed, 2)

    return final_result


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    logger.info("")
    logger.info("#" * 70)
    logger.info("PADDLEOCR PDF PROCESSOR STARTED")
    logger.info("#" * 70)

    try:

        logger.info(
            "Checking OCR API: %s",
            OCR_HEALTH_URL
        )

        # Longer timeout: the Azure app scales to zero and may need a cold
        # start (spin up + model load) before the health endpoint responds.
        health = requests.get(
            OCR_HEALTH_URL,
            timeout=300
        )

        health.raise_for_status()

        logger.info(
            "OCR API is running: %s",
            health.json()
        )

        result = process_pdf(PDF_FILE)

        logger.info(
            "Saving output to: %s",
            OUTPUT_FILE
        )

        with open(
            OUTPUT_FILE,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                result,
                file,
                indent=2,
                ensure_ascii=False
            )

        logger.info("")
        logger.info("#" * 70)
        logger.info("PROCESSING COMPLETED SUCCESSFULLY")
        logger.info("OUTPUT FILE: %s", OUTPUT_FILE)
        logger.info(
            "TOTAL TIME: %.2f seconds",
            result["total_processing_time_seconds"]
        )
        logger.info("#" * 70)

    except Exception as e:

        logger.exception(
            "PROCESSING FAILED: %s",
            str(e)
        )

        raise
