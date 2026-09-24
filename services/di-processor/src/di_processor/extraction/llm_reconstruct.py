"""Vision-LLM reconstruction of handwritten fields (ported from the POC).

Sends the page image + tiled OCR JSON + the fixed field-key list to the external
Azure OpenAI vision model (via ``dmer_common.openai_client``) and returns a
validated :class:`HandwrittenExtraction`.

Flow: build messages -> complete -> parse_llm_json -> sanitize_fields ->
validate_llm_output. Non-conforming output raises (Req 6.5).
"""

from __future__ import annotations

import base64
import json
from typing import Any

from dmer_common.openai_client import OpenAIClient
from dmer_common.telemetry import get_logger
from PIL import Image

from ..failures import FailureCode, failure_step
from .render import image_to_png_bytes
from .sanitize import (
    load_field_keys,
    load_known_form_labels,
    load_prompt,
    parse_llm_json,
    sanitize_fields,
)
from .schemas import HandwrittenExtraction, validate_llm_output

_log = get_logger(__name__)

MAX_COMPLETION_TOKENS = 16000
LLM_MAX_WIDTH = 1600


def _image_data_url(image: Image.Image, max_width: int = LLM_MAX_WIDTH) -> str:
    """Downscale (if wide) and encode the page image as a PNG data URL."""
    if image.width > max_width:
        scale = max_width / image.width
        image = image.resize((max_width, int(image.height * scale)), Image.LANCZOS)
    b64 = base64.b64encode(image_to_png_bytes(image)).decode("utf-8")
    return f"data:image/png;base64,{b64}"


def _template_labels_block() -> str:
    """Render the printed template labels as a prompt hint block."""
    from importlib import resources

    data = json.loads(
        resources.files("di_processor.extraction.resources")
        .joinpath("dmer_template_labels.json")
        .read_text(encoding="utf-8")
    )
    lines = [
        (
            "The following are the form's PRE-PRINTED template labels (from the "
            "blank form). Any text matching these is a printed label, NOT a "
            "handwritten value:"
        )
    ]
    for section in data.get("sections", []):
        labels = "; ".join(section.get("labels", []))
        lines.append(f"- {section.get('section', '')}: {labels}")
    return "\n".join(lines)


def build_messages(
    system_prompt: str,
    ocr_json: dict[str, Any],
    image_data_url: str,
    field_keys: tuple[str, ...],
) -> list[dict[str, Any]]:
    """Build the vision chat messages: field keys + labels + image + OCR JSON."""
    field_keys_block = "\n".join(f"- {k}" for k in field_keys)
    template_block = _template_labels_block()
    template_section = (
        f"\n\nTEMPLATE LABELS (printed form structure):\n{template_block}\n"
        if template_block
        else ""
    )
    return [
        {"role": "system", "content": system_prompt},
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": (
                        "Extract the handwritten values for the Driver's Medical "
                        "Examination page. The IMAGE is the primary source of truth; "
                        "the OCR JSON is a second opinion. Return a value for EVERY "
                        'field key below (use "" if blank).'
                        + "\n\nFIELD KEYS TO EXTRACT:\n"
                        + field_keys_block
                        + template_section
                        + "\n\nOCR JSON:\n```json\n"
                        + json.dumps(ocr_json, indent=2, ensure_ascii=False)
                        + "\n```"
                    ),
                },
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ],
        },
    ]


def reconstruct(
    client: OpenAIClient,
    page_image: Image.Image,
    ocr_json: dict[str, Any],
) -> HandwrittenExtraction:
    """Reconstruct handwritten fields from the page image + OCR JSON.

    Sends the vision request, parses/repairs the JSON, applies the sanitize
    safeguards, and validates against the schema (binary-confidence rule).
    """
    field_keys = load_field_keys()
    messages = build_messages(
        load_prompt(), ocr_json, _image_data_url(page_image), field_keys
    )
    with failure_step(FailureCode.LLM_CALL_FAILED):
        raw_text = client.complete(
            messages, max_completion_tokens=MAX_COMPLETION_TOKENS
        )
    # Unparseable or schema-violating output is the model's fault, not the call's.
    with failure_step(FailureCode.LLM_OUTPUT_INVALID):
        parsed = parse_llm_json(raw_text)
        sanitized = sanitize_fields(parsed, field_keys, load_known_form_labels())
        result = validate_llm_output(sanitized)
    _log.info(
        "handwritten reconstruction complete",
        extra={
            "field_count": len(result.fields),
            "uncertain_count": len(result.uncertain_fields),
        },
    )
    return result
