"""Dead-letter classification for the DLQ Drain.

See docs/development/stages/10-dlq-drain.md. Every dead-lettered message is
put in one :class:`~dmer_common.db.processing_error.FailureCategory`, and the
category decides what happens next:

- **retriable** (``TRANSIENT`` / ``PROCESSING``) -- a retry could succeed: a
  database or network blip, a dependency's 429/5xx, invalid model output, a
  lost lock, delivery attempts used up with no logical error behind them. The
  drain re-sends the message (bounded by ``DLQ_REDRIVE_MAX_ATTEMPTS``);
- ``PERMANENT_BUSINESS`` -- the document can never succeed (an unreadable
  PDF). The document goes to ``MANUAL_REVIEW`` with the fallback IN outcome
  (question I-1);
- ``UNKNOWN`` -- a logical error (an illegal status transition, a 4xx from a
  dependency, an unexpected exception) or a reason we can't map: the document
  goes to ``MANUAL_REVIEW`` for a person, with no decision.

Only ``PERMANENT_BUSINESS`` may ever produce a decision. Classification reads
the dead-letter ``reason`` / ``description`` (an application reason such as
the DI processor's ``FailureCode``, or a Service Bus system reason) and, for a
system reason, the document's last ``dmer_stage_run.error_code`` -- the
exception class name the stage recorded.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .db.processing_error import FailureCategory

# Reason codes (processing_error.reason_code; 10-dlq-drain.md "Reason codes").
PERMANENT_UNREADABLE_DOCUMENT: Final = "PERMANENT_UNREADABLE_DOCUMENT"
PERMANENT_DOCUMENT_GONE: Final = "PERMANENT_DOCUMENT_GONE"
TRANSIENT_LOCK_EXPIRED: Final = "TRANSIENT_LOCK_EXPIRED"
TRANSIENT_DB_FAILURE: Final = "TRANSIENT_DB_FAILURE"
TRANSIENT_STORAGE_FAILURE: Final = "TRANSIENT_STORAGE_FAILURE"
TRANSIENT_DELIVERY_EXHAUSTED: Final = "TRANSIENT_DELIVERY_EXHAUSTED"
PROCESSING_DEPENDENCY_THROTTLED: Final = "PROCESSING_DEPENDENCY_THROTTLED"
PROCESSING_DEPENDENCY_UNAVAILABLE: Final = "PROCESSING_DEPENDENCY_UNAVAILABLE"
PROCESSING_MODEL_OUTPUT_INVALID: Final = "PROCESSING_MODEL_OUTPUT_INVALID"
UNKNOWN_DEPENDENCY_REJECTED: Final = "UNKNOWN_DEPENDENCY_REJECTED"
UNKNOWN_LOGICAL_ERROR: Final = "UNKNOWN_LOGICAL_ERROR"
UNKNOWN_UNCLASSIFIED: Final = "UNKNOWN_UNCLASSIFIED"
UNKNOWN_REDRIVE_EXHAUSTED: Final = "UNKNOWN_REDRIVE_EXHAUSTED"

# The comment on a fallback IN, after "AI could not process." (I-1). Goes to
# Mercury, so it says what happened without any document content.
FALLBACK_REASONS: Final = {
    PERMANENT_UNREADABLE_DOCUMENT: "The DMER PDF could not be read.",
    PERMANENT_DOCUMENT_GONE: "The DMER no longer exists in Mercury.",
}

# Application dead-letter reasons (the DI processor's FailureCode values) ->
# (category, reason code). Dependency calls are classified by HTTP status
# instead -- see _DEPENDENCY_REASONS.
_APPLICATION_REASONS: Final = {
    "PDF_UNREADABLE": (
        FailureCategory.PERMANENT_BUSINESS,
        PERMANENT_UNREADABLE_DOCUMENT,
    ),
    "DB_READ_FAILED": (FailureCategory.TRANSIENT, TRANSIENT_DB_FAILURE),
    "DB_WRITE_FAILED": (FailureCategory.TRANSIENT, TRANSIENT_DB_FAILURE),
    "SOURCE_DOWNLOAD_FAILED": (FailureCategory.TRANSIENT, TRANSIENT_STORAGE_FAILURE),
    "ARTIFACT_WRITE_FAILED": (FailureCategory.TRANSIENT, TRANSIENT_STORAGE_FAILURE),
    "PUBLISH_FAILED": (FailureCategory.TRANSIENT, TRANSIENT_DELIVERY_EXHAUSTED),
    "LLM_OUTPUT_INVALID": (FailureCategory.PROCESSING, PROCESSING_MODEL_OUTPUT_INVALID),
    "INVALID_STATUS_TRANSITION": (FailureCategory.UNKNOWN, UNKNOWN_LOGICAL_ERROR),
    "STALE_STATUS": (FailureCategory.UNKNOWN, UNKNOWN_LOGICAL_ERROR),
    "UNEXPECTED": (FailureCategory.UNKNOWN, UNKNOWN_UNCLASSIFIED),
}
# Explicit reasons that are already reason codes pass through.
_REASON_CODE_CATEGORIES: Final = {
    PERMANENT_UNREADABLE_DOCUMENT: FailureCategory.PERMANENT_BUSINESS,
    PERMANENT_DOCUMENT_GONE: FailureCategory.PERMANENT_BUSINESS,
}

# Calls to Document Intelligence / the LLM: retriable unless the dependency
# rejected the request itself (a 4xx other than 408/429).
_DEPENDENCY_REASONS: Final = frozenset(
    {"DI_CUSTOM_MODEL_FAILED", "OCR_FAILED", "LLM_CALL_FAILED"}
)

# Service Bus system reasons.
_MAX_DELIVERY: Final = "MaxDeliveryCountExceeded"
_TTL_EXPIRED: Final = "TTLExpiredException"
_LOCK_LOST: Final = frozenset({"MessageLockLost", "SessionLockLost"})

# Stage-run error codes (exception class names) behind a delivery-count dead
# letter. A logical error is not retried; anything else is (bounded).
_LOGICAL_ERRORS: Final = frozenset(
    {
        "InvalidStatusTransition",
        "InvalidEvaluationTransition",
        "DriverDecisionStateError",
        "DriverLookupStateError",
        "ValidationError",
        "ValueError",
        "KeyError",
        "TypeError",
        "AttributeError",
        "JSONDecodeError",
        "IntegrityError",
    }
)
_DB_ERRORS: Final = frozenset(
    {
        "OperationalError",
        "InterfaceError",
        "DBAPIError",
        "ConnectionDoesNotExistError",
        "TooManyConnectionsError",
        "DeadlockDetectedError",
        "QueryCanceledError",
        "PostgresConnectionError",
    }
)

_HTTP_STATUS_RE = re.compile(r"http_status=(\d{3})")


@dataclass(frozen=True)
class Classification:
    """What failed (``category``) and why (``reason_code``)."""

    category: FailureCategory
    reason_code: str

    @property
    def retriable(self) -> bool:
        """A retry could succeed: the drain re-sends the message."""
        return self.category in (FailureCategory.TRANSIENT, FailureCategory.PROCESSING)


def _dependency(description: str) -> Classification:
    if "circuit_open=true" in description:
        return Classification(
            FailureCategory.PROCESSING, PROCESSING_DEPENDENCY_UNAVAILABLE
        )
    match = _HTTP_STATUS_RE.search(description)
    status = int(match.group(1)) if match else None
    if status == 429:
        return Classification(
            FailureCategory.PROCESSING, PROCESSING_DEPENDENCY_THROTTLED
        )
    if status is not None and 400 <= status < 500 and status != 408:
        return Classification(FailureCategory.UNKNOWN, UNKNOWN_DEPENDENCY_REJECTED)
    # 5xx, 408, or no status at all (a network error / timeout).
    return Classification(FailureCategory.PROCESSING, PROCESSING_DEPENDENCY_UNAVAILABLE)


def _delivery_exhausted(last_error_code: str | None) -> Classification:
    """Service Bus gave up after its delivery attempts: the stage's own last
    error says whether retrying could help."""
    code = (last_error_code or "").strip()
    if code in _LOGICAL_ERRORS:
        return Classification(FailureCategory.UNKNOWN, UNKNOWN_LOGICAL_ERROR)
    if code in _DB_ERRORS:
        return Classification(FailureCategory.TRANSIENT, TRANSIENT_DB_FAILURE)
    if code in _APPLICATION_REASONS or code in _DEPENDENCY_REASONS:
        return classify(code, "", None)
    # Timeouts, abandoned/interrupted runs, lost locks, a busy driver/document
    # lock, or nothing recorded: no logical error behind it, so retry.
    return Classification(FailureCategory.TRANSIENT, TRANSIENT_DELIVERY_EXHAUSTED)


def classify(
    reason: str | None, description: str | None, last_error_code: str | None
) -> Classification:
    """Classify one dead-lettered message.

    *reason* / *description* are the dead-letter reason and description;
    *last_error_code* is the document's last ``dmer_stage_run.error_code`` for
    the queue's stage (``None`` when there is none).
    """
    reason = (reason or "").strip()
    description = description or ""
    if reason in _REASON_CODE_CATEGORIES:
        return Classification(_REASON_CODE_CATEGORIES[reason], reason)
    if reason in _APPLICATION_REASONS:
        return Classification(*_APPLICATION_REASONS[reason])
    if reason in _DEPENDENCY_REASONS:
        return _dependency(description)
    if reason == _MAX_DELIVERY:
        return _delivery_exhausted(last_error_code)
    if reason == _TTL_EXPIRED:
        return Classification(FailureCategory.TRANSIENT, TRANSIENT_DELIVERY_EXHAUSTED)
    if reason in _LOCK_LOST:
        return Classification(FailureCategory.TRANSIENT, TRANSIENT_LOCK_EXPIRED)
    return Classification(FailureCategory.UNKNOWN, UNKNOWN_UNCLASSIFIED)
