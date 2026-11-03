"""Dead-letter classification (stages/10-dlq-drain.md): retry what a retry
could fix, give a permanent failure the fallback, send the rest to a person."""

import pytest
from dmer_common.db.processing_error import FailureCategory
from dmer_common.dlq import classify

P, T, PR, U = (
    FailureCategory.PERMANENT_BUSINESS,
    FailureCategory.TRANSIENT,
    FailureCategory.PROCESSING,
    FailureCategory.UNKNOWN,
)


@pytest.mark.parametrize(
    ("reason", "description", "category", "code"),
    [
        ("PDF_UNREADABLE", "error=PdfReadError", P, "PERMANENT_UNREADABLE_DOCUMENT"),
        ("PERMANENT_DOCUMENT_GONE", "", P, "PERMANENT_DOCUMENT_GONE"),
        ("DB_WRITE_FAILED", "error=OperationalError", T, "TRANSIENT_DB_FAILURE"),
        (
            "SOURCE_DOWNLOAD_FAILED",
            "error=ServiceRequestError",
            T,
            "TRANSIENT_STORAGE_FAILURE",
        ),
        (
            "LLM_OUTPUT_INVALID",
            "error=ValidationError",
            PR,
            "PROCESSING_MODEL_OUTPUT_INVALID",
        ),
        ("INVALID_STATUS_TRANSITION", "", U, "UNKNOWN_LOGICAL_ERROR"),
        ("UNEXPECTED", "error=KeyError", U, "UNKNOWN_UNCLASSIFIED"),
        ("MessageLockLost", "", T, "TRANSIENT_LOCK_EXPIRED"),
        ("TTLExpiredException", "", T, "TRANSIENT_DELIVERY_EXHAUSTED"),
        (None, None, U, "UNKNOWN_UNCLASSIFIED"),
        ("something new", "", U, "UNKNOWN_UNCLASSIFIED"),
    ],
)
def test_application_and_system_reasons(reason, description, category, code):
    result = classify(reason, description, None)
    assert (result.category, result.reason_code) == (category, code)


@pytest.mark.parametrize(
    ("description", "category", "code"),
    [
        (
            "error=HttpResponseError; http_status=429",
            PR,
            "PROCESSING_DEPENDENCY_THROTTLED",
        ),
        (
            "error=HttpResponseError; http_status=503",
            PR,
            "PROCESSING_DEPENDENCY_UNAVAILABLE",
        ),
        (
            "error=HttpResponseError; http_status=408",
            PR,
            "PROCESSING_DEPENDENCY_UNAVAILABLE",
        ),
        (
            "error=CircuitOpenError; circuit_open=true",
            PR,
            "PROCESSING_DEPENDENCY_UNAVAILABLE",
        ),
        ("error=ServiceRequestError", PR, "PROCESSING_DEPENDENCY_UNAVAILABLE"),
        # the dependency rejected the request itself: a person looks at it
        ("error=HttpResponseError; http_status=400", U, "UNKNOWN_DEPENDENCY_REJECTED"),
    ],
)
def test_dependency_calls_are_classified_by_http_status(description, category, code):
    for reason in ("DI_CUSTOM_MODEL_FAILED", "OCR_FAILED", "LLM_CALL_FAILED"):
        result = classify(reason, description, None)
        assert (result.category, result.reason_code) == (category, code)


@pytest.mark.parametrize(
    ("last_error", "category", "code"),
    [
        # no logical error behind it: retry
        (None, T, "TRANSIENT_DELIVERY_EXHAUSTED"),
        ("ABANDONED", T, "TRANSIENT_DELIVERY_EXHAUSTED"),
        ("TimeoutError", T, "TRANSIENT_DELIVERY_EXHAUSTED"),
        ("DriverDecisionBusyError", T, "TRANSIENT_DELIVERY_EXHAUSTED"),
        ("OperationalError", T, "TRANSIENT_DB_FAILURE"),
        ("PDF_UNREADABLE", P, "PERMANENT_UNREADABLE_DOCUMENT"),
        # a logical error: retrying won't fix it
        ("InvalidStatusTransition", U, "UNKNOWN_LOGICAL_ERROR"),
        ("ValidationError", U, "UNKNOWN_LOGICAL_ERROR"),
    ],
)
def test_max_delivery_count_uses_the_stage_s_last_error(last_error, category, code):
    result = classify("MaxDeliveryCountExceeded", "", last_error)
    assert (result.category, result.reason_code) == (category, code)


def test_only_transient_and_processing_are_retriable():
    assert classify("DB_READ_FAILED", "", None).retriable
    assert classify("LLM_OUTPUT_INVALID", "", None).retriable
    assert not classify("PDF_UNREADABLE", "", None).retriable
    assert not classify("UNEXPECTED", "", None).retriable
