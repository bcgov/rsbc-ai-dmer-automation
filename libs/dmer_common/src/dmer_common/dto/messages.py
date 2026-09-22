"""Queue message models for the DMER pipeline.

Single source of truth for the schemas documented in ``docs/contracts/queues/``:

- :class:`RawDmerMessage` — ``raw-dmer-queue`` (produced by intake-processor,
  consumed by di-processor).
- :class:`ExtractedDmerMessage` — ``extracted-dmer-queue`` v2 (produced by
  di-processor, consumed by workflow-orchestrator). v2 replaces ``ocrResultUri``
  with ``combinedResultUri`` and bumps ``schemaVersion`` to ``2.0``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from .envelope import Envelope

RAW_DMER_SCHEMA_VERSION = "1.0"
EXTRACTED_DMER_SCHEMA_VERSION = "2.0"


class RawDmerMessage(Envelope):
    """``raw-dmer-queue`` message.

    See ``docs/contracts/queues/raw-dmer-queue.md``. ``message_id`` is the
    idempotency key; di-processor must no-op on a duplicate it has completed.
    ``document_id`` is inherited from :class:`Envelope`.
    """

    source_system: str
    mercury_case_id: str
    document_uri: str
    received_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ExtractedDmerMessage(Envelope):
    """``extracted-dmer-queue`` v2 message.

    See ``docs/contracts/queues/extracted-dmer-queue.md``. References the final
    combined extraction result rather than a raw OCR result. ``document_id``
    is inherited from :class:`Envelope`.
    """

    schema_version: str = EXTRACTED_DMER_SCHEMA_VERSION
    mercury_case_id: str
    sha256_hash: str
    combined_result_uri: str
    processed_at: datetime
