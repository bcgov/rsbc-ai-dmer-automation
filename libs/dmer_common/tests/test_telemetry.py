"""Unit tests for structured JSON logging: PII redaction and correlation id.

Behaviour specs (GIVEN/WHEN/THEN) for the redaction helper, the logging filters,
the JSON formatter, and correlation-id context propagation.
"""

from __future__ import annotations

import json
import logging

from dmer_common.telemetry import (
    JsonFormatter,
    correlation_context,
    get_correlation_id,
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


def test_logger_emits_json_with_correlation_id():
    # GIVEN a logger and an ambient correlation id
    logger = get_logger("test.telemetry.corr")
    buffer = _capture(logger)
    # WHEN logging inside a correlation context
    with correlation_context("case-abc"):
        logger.info("processing", extra={"document_id": "doc-1"})
    # THEN the emitted line is JSON carrying the correlation id and safe extras
    line = buffer.getvalue().strip()
    record = json.loads(line)
    assert record["correlation_id"] == "case-abc"
    assert record["message"] == "processing"
    assert record["document_id"] == "doc-1"
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


def test_correlation_context_restores_previous_value():
    # GIVEN no ambient correlation id
    assert get_correlation_id() is None
    # WHEN entering and leaving a context
    with correlation_context("case-1"):
        assert get_correlation_id() == "case-1"
    # THEN the previous (empty) value is restored
    assert get_correlation_id() is None


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
