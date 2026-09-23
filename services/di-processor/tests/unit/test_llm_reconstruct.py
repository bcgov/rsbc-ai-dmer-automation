"""Unit tests for vision-LLM reconstruction (mocked OpenAI client) (Req 6.1, 6.2, 6.5)."""

from __future__ import annotations

import json

import pytest
from di_processor.extraction.llm_reconstruct import build_messages, reconstruct
from di_processor.extraction.schemas import ExtractionError, HandwrittenExtraction
from PIL import Image


def _ocr_json():
    return {"pages": [{"page_number": 1, "rows": []}]}


def test_build_messages_includes_image_keys_and_ocr():
    msgs = build_messages(
        "SYSTEM", _ocr_json(), "data:image/png;base64,AAA", ("endocrine.HbA1C",)
    )
    assert msgs[0]["role"] == "system"
    user = msgs[1]["content"]
    text_block = next(b for b in user if b["type"] == "text")["text"]
    assert "endocrine.HbA1C" in text_block
    assert "OCR JSON" in text_block
    image_block = next(b for b in user if b["type"] == "image_url")
    assert image_block["image_url"]["url"].startswith("data:image/png;base64,")


class _FakeOpenAI:
    def __init__(self, payload):
        self._payload = payload
        self.calls = 0

    def complete(self, messages, **kwargs):
        self.calls += 1
        return self._payload


def _valid_llm_payload():
    return json.dumps(
        {
            "fields": {
                "endocrine.HbA1C": {
                    "value": "6.5",
                    "confidence": "high",
                    "source": "both",
                    "notes": "",
                }
            },
            "uncertain_fields": [],
        }
    )


def test_reconstruct_returns_validated_extraction():
    client = _FakeOpenAI(_valid_llm_payload())
    img = Image.new("RGB", (100, 100), "white")
    result = reconstruct(client, img, _ocr_json())
    assert isinstance(result, HandwrittenExtraction)
    assert result.fields["endocrine.HbA1C"].value == "6.5"
    # all 67 keys filled by sanitize
    assert len(result.fields) == 67


def test_reconstruct_sanitizes_binary_confidence_before_validation():
    # GIVEN the LLM returns high confidence with source != both (a violation)
    bad = json.dumps(
        {
            "fields": {
                "endocrine.HbA1C": {
                    "value": "6.5",
                    "confidence": "high",
                    "source": "ocr",
                }
            }
        }
    )
    client = _FakeOpenAI(bad)
    img = Image.new("RGB", (10, 10), "white")
    # WHEN reconstructed, sanitize downgrades it to low so validation passes
    result = reconstruct(client, img, _ocr_json())
    assert result.fields["endocrine.HbA1C"].confidence.value == "low"


def test_reconstruct_rejects_unrepairable_output():
    # GIVEN LLM output with an invalid enum that sanitize does not repair
    bad = json.dumps(
        {"fields": {"endocrine.HbA1C": {"value": "6.5", "source": "banana"}}}
    )
    client = _FakeOpenAI(bad)
    img = Image.new("RGB", (10, 10), "white")
    # WHEN reconstructed THEN validation raises (Req 6.5)
    with pytest.raises(ExtractionError):
        reconstruct(client, img, _ocr_json())
