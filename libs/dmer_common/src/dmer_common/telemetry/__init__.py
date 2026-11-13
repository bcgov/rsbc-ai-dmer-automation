"""Structured JSON logging for every DMER service.

Provides a single ``get_logger`` factory that emits one JSON object per log line,
always carrying a ``document_id`` and redacting personal/medical (PII) fields
at the logging layer (never rely on downstream scrubbing — DMER content is
personal/medical information under FOIPPA).

Usage::

    from dmer_common.telemetry import get_logger, document_id_context

    log = get_logger(__name__)
    with document_id_context("doc-1"):
        log.info("processing document")

Design notes:
- No bare ``print`` anywhere in the codebase — this is the only logging surface.
- ``document_id`` (``dmer_document.id`` — see ``docs/development/data-model.md``)
  is injected from a ``contextvars`` context so it flows through async tasks
  without threading it through every call. There is no separate correlation
  id: document_id already is the one stable, generated-once-at-ingest id
  every log line/message needs.
- PII redaction runs in a logging ``Filter`` so it applies uniformly regardless
  of how a record is produced.
"""

from __future__ import annotations

from .logging import (
    DEFAULT_PII_FIELDS,
    DocumentIdFilter,
    JsonFormatter,
    PiiRedactionFilter,
    bind_document_id,
    document_id_context,
    get_document_id,
    get_logger,
    redact,
)

__all__ = [
    "DEFAULT_PII_FIELDS",
    "DocumentIdFilter",
    "JsonFormatter",
    "PiiRedactionFilter",
    "bind_document_id",
    "document_id_context",
    "get_document_id",
    "get_logger",
    "redact",
]
