"""Pydantic models and validation for the di-processor extraction stages.

Three artifacts are modeled:

- **Top-level fields** (Stage A) — output of the custom-trained Document
  Intelligence model. The trained model's exact field set is configuration, so
  this is a permissive typed container (``TopLevelExtraction``) keyed by field
  name, each carrying a value and optional confidence.
- **Handwritten fields** (Stage C) — the vision-LLM reconstruction: a flat map
  keyed by the fixed field keys (see ``resources/dmer_field_schema.json``), each
  a :class:`HandwrittenField`, plus ``uncertain_fields``.
- **Combined extraction** (Stage D) — the unified merge of the two, with
  document-level metadata.

``validate_llm_output`` enforces the handwritten shape and the project's
**binary-confidence rule**: confidence is ``high`` only when image and OCR agree
(``source == "both"``); every other source must be ``low`` (never ``medium``).
This satisfies Requirement 6.5 (malformed / non-conforming LLM output is a
processing failure, not silently accepted).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class ExtractionError(ValueError):
    """Raised when an extraction artifact does not conform to its schema."""


class Confidence(str, Enum):
    """Binary confidence. ``medium`` is intentionally not a member."""

    HIGH = "high"
    LOW = "low"


class Source(str, Enum):
    """Where a handwritten value came from.

    ``both`` means image and OCR agree (the only case that permits ``high``
    confidence); ``none`` means the field is blank.
    """

    BOTH = "both"
    IMAGE = "image"
    OCR = "ocr"
    NONE = "none"


class HandwrittenField(BaseModel):
    """A single reconstructed handwritten field entry."""

    model_config = ConfigDict(extra="forbid")

    value: str = ""
    confidence: Confidence = Confidence.LOW
    source: Source = Source.NONE
    notes: str = ""

    @model_validator(mode="after")
    def _enforce_binary_confidence(self) -> HandwrittenField:
        # high confidence is only valid when image and OCR agree (source == both)
        if self.confidence is Confidence.HIGH and self.source is not Source.BOTH:
            raise ValueError(
                "confidence 'high' requires source 'both' (image and OCR agree)"
            )
        return self


class HandwrittenExtraction(BaseModel):
    """The LLM reconstruction output: field map + uncertain-field list."""

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, HandwrittenField] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)


class TopLevelField(BaseModel):
    """A field emitted by the custom-trained DI model."""

    model_config = ConfigDict(extra="forbid")

    value: str = ""
    confidence: float | None = None


class TopLevelExtraction(BaseModel):
    """Custom-model top-level output.

    The trained model's field set is configuration; kept permissive so a model
    revision that adds fields does not break parsing. Values are normalized to
    :class:`TopLevelField`.
    """

    model_config = ConfigDict(extra="forbid")

    fields: dict[str, TopLevelField] = Field(default_factory=dict)


class CombinedExtraction(BaseModel):
    """Unified combined extraction persisted to ``combined-extracted-dmer``.

    Top-level and handwritten fields are merged into a single flat ``fields`` map
    (top-level keys namespaced under ``top_level.``; handwritten keys as-is),
    with document-level metadata.
    """

    model_config = ConfigDict(extra="forbid")

    document_id: str
    correlation_id: str
    source_model_version: str | None = None
    prompt_version: str | None = None
    processed_at: datetime
    fields: dict[str, Any] = Field(default_factory=dict)
    uncertain_fields: list[str] = Field(default_factory=list)


def validate_llm_output(raw: dict[str, Any]) -> HandwrittenExtraction:
    """Validate raw LLM output into a :class:`HandwrittenExtraction`.

    Raises :class:`ExtractionError` on any non-conformance (unknown fields, bad
    enum values, or a binary-confidence violation) so the pipeline treats it as a
    processing failure rather than passing malformed data downstream (Req 6.5).
    """
    try:
        return HandwrittenExtraction.model_validate(raw)
    except ValidationError as exc:
        raise ExtractionError(f"LLM output failed schema validation: {exc}") from exc
