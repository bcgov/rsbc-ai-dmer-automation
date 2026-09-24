"""Failure codes and PII-safe failure details for the extraction pipeline.

Every failure is reported with one stable :class:`FailureCode` — chosen by
*which step* failed, not by the exception type (the same type comes from many
steps, and retries / the circuit breaker re-raise generic errors). The same code
and detail go to ``dmer_stage_run.error_code`` / ``error_detail``, the Service
Bus dead-letter ``reason`` / ``description``, and the error log.

The detail is built only from safe facts — the exception *type*, an HTTP status
when the error carries one, and whether a circuit breaker was open — never from
the exception *message*, which can contain extracted licence or clinical values
(e.g. a schema-validation error echoes the offending field values). Format::

    error=ResourceNotFoundError; http_status=404
"""

from __future__ import annotations

import enum
from collections.abc import Iterator
from contextlib import contextmanager

from dmer_common.db import InvalidStatusTransition, StaleStatusError
from dmer_common.retry import CircuitOpenError


class FailureCode(str, enum.Enum):
    """Why an extraction run failed (``dmer_stage_run.error_code``)."""

    DB_READ_FAILED = "DB_READ_FAILED"
    DB_WRITE_FAILED = "DB_WRITE_FAILED"
    INVALID_STATUS_TRANSITION = "INVALID_STATUS_TRANSITION"
    # Not a document failure: another worker changed the status first (lost a
    # compare-and-set race). The pipeline stops quietly on it.
    STALE_STATUS = "STALE_STATUS"
    SOURCE_DOWNLOAD_FAILED = "SOURCE_DOWNLOAD_FAILED"
    PDF_UNREADABLE = "PDF_UNREADABLE"
    DI_CUSTOM_MODEL_FAILED = "DI_CUSTOM_MODEL_FAILED"
    OCR_FAILED = "OCR_FAILED"
    LLM_CALL_FAILED = "LLM_CALL_FAILED"
    LLM_OUTPUT_INVALID = "LLM_OUTPUT_INVALID"
    ARTIFACT_WRITE_FAILED = "ARTIFACT_WRITE_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    EXTRACTED_POINTER_MISSING = "EXTRACTED_POINTER_MISSING"
    UNEXPECTED = "UNEXPECTED"


class PipelineFailure(Exception):
    """A classified, PII-safe pipeline failure.

    ``str()`` is the code plus the safe detail — never the underlying message.
    The original exception stays available as ``__cause__`` for local debugging
    but must not be logged. ``dead_letter_reason`` / ``safe_detail`` are the
    attributes the shared Service Bus consumer reads when dead-lettering.
    """

    def __init__(self, code: FailureCode, safe_detail: str) -> None:
        super().__init__(f"{code.value}: {safe_detail}")
        self.code = code
        self.safe_detail = safe_detail

    @property
    def dead_letter_reason(self) -> str:
        return self.code.value


def safe_detail(exc: BaseException, **facts: object) -> str:
    """Build the ``key=value; ...`` detail from ``exc``'s type plus safe facts."""
    parts = [f"error={type(exc).__name__}"]
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        parts.append(f"http_status={status}")
    if isinstance(exc, CircuitOpenError):
        parts.append("circuit_open=true")
    parts.extend(f"{key}={value}" for key, value in facts.items())
    return "; ".join(parts)


def as_failure(exc: BaseException, code: FailureCode) -> PipelineFailure:
    """Classify ``exc`` as a :class:`PipelineFailure` under ``code``.

    An already-classified failure passes through unchanged (the innermost step
    wins), and an illegal status transition or a lost compare-and-set race is
    always reported as such.
    """
    if isinstance(exc, PipelineFailure):
        return exc
    if isinstance(exc, InvalidStatusTransition):
        code = FailureCode.INVALID_STATUS_TRANSITION
    if isinstance(exc, StaleStatusError):
        # Status names are safe (not PII), and say who won the race.
        expected = exc.expected.value if exc.expected else "none"
        actual = exc.actual.value if exc.actual else "none"
        return PipelineFailure(
            FailureCode.STALE_STATUS,
            safe_detail(exc, expected=expected, actual=actual),
        )
    return PipelineFailure(code, safe_detail(exc))


@contextmanager
def failure_step(code: FailureCode) -> Iterator[None]:
    """Run a step so any failure inside it is reported as ``code``."""
    try:
        yield
    except Exception as exc:
        failure = as_failure(exc, code)
        if failure is exc:
            raise
        raise failure from exc
