"""Structured JSON logging implementation with document-id and PII redaction.

Kept dependency-free (stdlib ``logging`` + ``json`` + ``contextvars``) so it is
trivially unit-testable and adds no import weight to services.
"""

from __future__ import annotations

import contextvars
import json
import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Final

# The per-document trace id flows through async tasks via a context variable
# so it does not have to be threaded through every function call. This is
# dmer_document.id (the internal PK, constant for the document's life) --
# see docs/development/data-model.md. A dedicated correlation_id field was
# considered and dropped: document_id already serves the same purpose (one
# stable id per document, generated once, present on every message) with no
# separate field needed.
_document_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "document_id", default=None
)

# Field/key names treated as personal/medical information. Any log record attribute
# or nested mapping key matching one of these (case-insensitive) is redacted.
DEFAULT_PII_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "name",
        "first_name",
        "last_name",
        "full_name",
        "patient_name",
        "physician_name",
        "dob",
        "date_of_birth",
        "birth_date",
        "phn",
        "personal_health_number",
        "sin",
        "address",
        "street_address",
        "postal_code",
        "phone",
        "phone_number",
        "email",
        "diagnosis",
        "medical_history",
        "handwritten",
        "fields",
        "ocr_text",
        "content",
    }
)

_REDACTED: Final = "[REDACTED]"

# Standard LogRecord attributes we never emit as top-level custom fields.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        "document_id",
    }
)


def bind_document_id(document_id: str | None) -> contextvars.Token[str | None]:
    """Set the ambient document id; returns a token to restore the previous one."""
    return _document_id.set(document_id)


def get_document_id() -> str | None:
    """Return the document id bound to the current context, if any."""
    return _document_id.get()


@contextmanager
def document_id_context(document_id: str | None) -> Iterator[None]:
    """Bind ``document_id`` for the duration of the ``with`` block."""
    token = bind_document_id(document_id)
    try:
        yield
    finally:
        _document_id.reset(token)


def redact(
    value: Any, pii_fields: frozenset[str] = DEFAULT_PII_FIELDS, *, _depth: int = 0
) -> Any:
    """Recursively redact PII-named keys in mappings/sequences.

    Values whose *key* matches a PII field name are replaced with ``[REDACTED]``.
    Non-PII scalars pass through unchanged. Depth is bounded to avoid pathological
    structures dominating a log call.
    """
    if _depth > 6:
        return _REDACTED
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and key.lower() in pii_fields:
                redacted[key] = _REDACTED
            else:
                redacted[key] = redact(item, pii_fields, _depth=_depth + 1)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact(item, pii_fields, _depth=_depth + 1) for item in value]
    return value


class PiiRedactionFilter(logging.Filter):
    """Logging filter that redacts PII-named ``extra`` fields on every record."""

    def __init__(self, pii_fields: frozenset[str] = DEFAULT_PII_FIELDS) -> None:
        super().__init__()
        self._pii_fields = pii_fields

    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in list(record.__dict__.items()):
            if key in _RESERVED_RECORD_ATTRS:
                continue
            if key.lower() in self._pii_fields:
                record.__dict__[key] = _REDACTED
            else:
                record.__dict__[key] = redact(value, self._pii_fields)
        return True


class DocumentIdFilter(logging.Filter):
    """Injects the ambient document id onto every record as ``document_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "document_id", None):
            record.document_id = get_document_id()
        return True


class JsonFormatter(logging.Formatter):
    """Render a ``LogRecord`` as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "document_id": getattr(record, "document_id", None),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key in payload:
                continue
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def get_logger(
    name: str,
    *,
    level: int = logging.INFO,
    pii_fields: frozenset[str] = DEFAULT_PII_FIELDS,
) -> logging.Logger:
    """Return a logger configured for structured JSON output with PII redaction.

    Idempotent: repeated calls for the same ``name`` do not stack handlers.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not any(getattr(h, "_dmer_common", False) for h in logger.handlers):
        handler = logging.StreamHandler()
        handler._dmer_common = True  # type: ignore[attr-defined]
        handler.setFormatter(JsonFormatter())
        handler.addFilter(PiiRedactionFilter(pii_fields))
        handler.addFilter(DocumentIdFilter())
        logger.addHandler(handler)
    return logger
