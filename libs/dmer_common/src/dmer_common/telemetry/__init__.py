"""Structured JSON logging for every DMER service.

Provides a single ``get_logger`` factory that emits one JSON object per log line,
always carrying a ``correlation_id`` and redacting personal/medical (PII) fields
at the logging layer (never rely on downstream scrubbing — DMER content is
personal/medical information under FOIPPA).

Usage::

    from dmer_common.telemetry import get_logger, correlation_context

    log = get_logger(__name__)
    with correlation_context("case-123"):
        log.info("processing document", extra={"document_id": "doc-1"})

Design notes:
- No bare ``print`` anywhere in the codebase — this is the only logging surface.
- ``correlation_id`` is injected from a ``contextvars`` context so it flows
  through async tasks without threading it through every call.
- PII redaction runs in a logging ``Filter`` so it applies uniformly regardless
  of how a record is produced.
"""

from __future__ import annotations

from .logging import (
    DEFAULT_PII_FIELDS,
    CorrelationIdFilter,
    JsonFormatter,
    PiiRedactionFilter,
    bind_correlation_id,
    correlation_context,
    get_correlation_id,
    get_logger,
    redact,
)

__all__ = [
    "DEFAULT_PII_FIELDS",
    "CorrelationIdFilter",
    "JsonFormatter",
    "PiiRedactionFilter",
    "bind_correlation_id",
    "correlation_context",
    "get_correlation_id",
    "get_logger",
    "redact",
]
