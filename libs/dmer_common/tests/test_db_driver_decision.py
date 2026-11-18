"""Unit tests for the driver_evaluation status state machine (pure, no database)."""

from __future__ import annotations

import pytest
from dmer_common.db.driver_decision import (
    EvaluationStatus,
    InvalidEvaluationTransition,
    validated_evaluation_status,
)

_E = EvaluationStatus


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (_E.WAITING, _E.READY),
        (_E.WAITING, _E.STALE),
        (_E.STALE, _E.READY),
        (_E.STALE, _E.WAITING),
        (_E.READY, _E.EVALUATING),
        (_E.READY, _E.WAITING),  # a fresh Mercury check found a new document
        (_E.EVALUATING, _E.DECIDED),
        (_E.EVALUATING, _E.WAITING),  # the attempt failed; the sweeper re-signals
        (_E.DECIDED, _E.POSTED),
    ],
)
def test_allowed_moves(current, target):
    assert validated_evaluation_status(current, target) is target


@pytest.mark.parametrize("status", list(EvaluationStatus))
def test_same_status_is_a_no_op(status):
    assert validated_evaluation_status(status, status) is status


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (_E.WAITING, _E.EVALUATING),  # must be READY first
        (_E.WAITING, _E.DECIDED),
        (_E.READY, _E.DECIDED),
        (_E.DECIDED, _E.WAITING),  # a decided batch is never reopened
        (_E.POSTED, _E.WAITING),
        (_E.POSTED, _E.DECIDED),
    ],
)
def test_illegal_moves_raise(current, target):
    with pytest.raises(InvalidEvaluationTransition):
        validated_evaluation_status(current, target)
