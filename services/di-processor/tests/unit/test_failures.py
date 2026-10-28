"""Unit tests for failure classification and PII-safe details."""

from __future__ import annotations

import pytest
from di_processor.failures import (
    FailureCode,
    PipelineFailure,
    as_failure,
    failure_step,
    safe_detail,
)
from dmer_common.db import InvalidStatusTransition
from dmer_common.retry import CircuitOpenError


class _HttpError(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def test_detail_is_type_only_never_the_message():
    detail = safe_detail(ValueError("licence 01234567"))
    assert detail == "error=ValueError"


def test_detail_carries_http_status_and_circuit_state():
    assert safe_detail(_HttpError("x", 404)) == "error=_HttpError; http_status=404"
    assert (
        safe_detail(CircuitOpenError("open"))
        == "error=CircuitOpenError; circuit_open=true"
    )


def test_non_int_status_is_ignored():
    err = _HttpError("x", "404")
    assert safe_detail(err) == "error=_HttpError"


def test_failure_step_classifies_and_chains():
    with (
        pytest.raises(PipelineFailure) as info,
        failure_step(FailureCode.PUBLISH_FAILED),
    ):
        raise RuntimeError("secret body")
    assert info.value.code is FailureCode.PUBLISH_FAILED
    assert str(info.value) == "PUBLISH_FAILED: error=RuntimeError"
    assert isinstance(info.value.__cause__, RuntimeError)


def test_innermost_classification_wins():
    # GIVEN an inner step already classified the failure
    with (
        pytest.raises(PipelineFailure) as info,
        failure_step(FailureCode.UNEXPECTED),
        failure_step(FailureCode.OCR_FAILED),
    ):
        raise RuntimeError("x")
    # THEN the outer step does not re-label it
    assert info.value.code is FailureCode.OCR_FAILED


def test_illegal_transition_always_has_its_own_code():
    failure = as_failure(InvalidStatusTransition("x"), FailureCode.DB_WRITE_FAILED)
    assert failure.code is FailureCode.INVALID_STATUS_TRANSITION


def test_dead_letter_attributes_for_the_consumer():
    failure = PipelineFailure(FailureCode.PDF_UNREADABLE, "error=IndexError")
    assert failure.dead_letter_reason == "PDF_UNREADABLE"
    assert failure.safe_detail == "error=IndexError"
