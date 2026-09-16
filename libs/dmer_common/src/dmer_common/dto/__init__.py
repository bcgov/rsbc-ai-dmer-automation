"""Shared Pydantic message/data-transfer objects (queue envelopes, document metadata,
normalization results, rule decisions) — the single source of truth for schemas
referenced in docs/contracts/queues/."""

from .envelope import Envelope
from .messages import (
    EXTRACTED_DMER_SCHEMA_VERSION,
    RAW_DMER_SCHEMA_VERSION,
    ExtractedDmerMessage,
    RawDmerMessage,
)

__all__ = [
    "EXTRACTED_DMER_SCHEMA_VERSION",
    "RAW_DMER_SCHEMA_VERSION",
    "Envelope",
    "ExtractedDmerMessage",
    "RawDmerMessage",
]
