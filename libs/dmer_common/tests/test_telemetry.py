"""Unit tests for structured JSON logging: PII redaction and document id.

Behaviour specs (GIVEN/WHEN/THEN) for the redaction helper, the logging filters,
the JSON formatter, and document-id context propagation.
"""

from __future__ import annotations

import json
import logging

from dmer_common.telemetry import (
    JsonFormatter,
    document_id_context,
    get_document_id,
    get_logger,
    redact,
)


def test_redact_masks_pii_keys_recursively():
    # GIVEN a nested structure containing PII-named keys
    payload = {
        "document_id": "doc-1",
        "patient_name": "Jane Doe",
        "nested": {"dob": "1990-01-01", "safe": "ok"},
        "list": [{"email": "a@b.ca"}, {"count": 3}],
    }
    # WHEN redacting
    result = redact(payload)
    # THEN PII values are masked and non-PII values are preserved
    assert result["document_id"] == "doc-1"
    assert result["patient_name"] == "[REDACTED]"
    assert result["nested"] == {"dob": "[REDACTED]", "safe": "ok"}
    assert result["list"] == [{"email": "[REDACTED]"}, {"count": 3}]


def _capture(logger: logging.Logger) -> list[str]:
    # Redirect the configured handler to an in-memory buffer to inspect output.
    import io

    buffer = io.StringIO()
    handler = logger.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)
    return buffer  # type: ignore[return-value]


def test_logger_emits_json_with_document_id():
    # GIVEN a logger and an ambient document id
    logger = get_logger("test.telemetry.docid")
    buffer = _capture(logger)
    # WHEN logging inside a document-id context
    with document_id_context("doc-abc"):
        logger.info("processing")
    # THEN the emitted line is JSON carrying the document id
    line = buffer.getvalue().strip()
    record = json.loads(line)
    assert record["document_id"] == "doc-abc"
    assert record["message"] == "processing"
    assert record["level"] == "INFO"


def test_logger_redacts_pii_extra_fields():
    # GIVEN a logger
    logger = get_logger("test.telemetry.pii")
    buffer = _capture(logger)
    # WHEN logging with a PII-named extra field
    logger.warning("saw patient", extra={"patient_name": "Jane Doe"})
    # THEN the PII value is redacted in the emitted JSON
    record = json.loads(buffer.getvalue().strip())
    assert record["patient_name"] == "[REDACTED]"


def test_document_id_context_restores_previous_value():
    # GIVEN no ambient document id
    assert get_document_id() is None
    # WHEN entering and leaving a context
    with document_id_context("doc-1"):
        assert get_document_id() == "doc-1"
    # THEN the previous (empty) value is restored
    assert get_document_id() is None


def test_get_logger_is_idempotent_no_duplicate_handlers():
    # GIVEN a logger fetched twice
    a = get_logger("test.telemetry.idem")
    b = get_logger("test.telemetry.idem")
    # THEN it is the same logger with a single handler (no stacking)
    assert a is b
    assert len([h for h in a.handlers if getattr(h, "_dmer_common", False)]) == 1


def test_json_formatter_includes_exception_text():
    # GIVEN a record with exception info
    formatter = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "n",
            logging.ERROR,
            __file__,
            1,
            "failed",
            None,
            __import__("sys").exc_info(),
        )
    # WHEN formatting THEN the exception text is included
    out = json.loads(formatter.format(record))
    assert "boom" in out["exception"]
