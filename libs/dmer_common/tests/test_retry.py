"""Unit tests for retry policies and the circuit breaker.

Behaviour specs (GIVEN/WHEN/THEN) for retry-on-failure, exhaustion re-raise,
and breaker open/half-open/close transitions.
"""

from __future__ import annotations

import pytest
from dmer_common.retry import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    retry_policy,
)


def test_retry_policy_retries_then_succeeds():
    # GIVEN a flaky function that fails twice then succeeds
    calls = {"n": 0}

    @retry_policy(max_attempts=3, initial_wait=0.0, max_wait=0.0)
    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ValueError("transient")
        return "ok"

    # WHEN invoked THEN it retries and eventually returns
    assert flaky() == "ok"
    assert calls["n"] == 3


def test_retry_policy_reraises_after_exhaustion():
    # GIVEN a function that always fails
    calls = {"n": 0}

    @retry_policy(max_attempts=2, initial_wait=0.0, max_wait=0.0)
    def always_fail() -> None:
        calls["n"] += 1
        raise ValueError("permanent")

    # WHEN invoked THEN the final error is re-raised after max attempts
    with pytest.raises(ValueError, match="permanent"):
        always_fail()
    assert calls["n"] == 2


def test_retry_policy_does_not_retry_unlisted_exception():
    # GIVEN a policy that only retries ValueError
    calls = {"n": 0}

    @retry_policy(max_attempts=3, initial_wait=0.0, retry_on=(ValueError,))
    def raise_key_error() -> None:
        calls["n"] += 1
        raise KeyError("nope")

    # WHEN a non-matching exception is raised THEN it propagates without retry
    with pytest.raises(KeyError):
        raise_key_error()
    assert calls["n"] == 1


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def test_breaker_opens_after_threshold_and_rejects():
    # GIVEN a breaker that trips after 2 failures
    breaker = CircuitBreaker(failure_threshold=2, reset_timeout=10.0, clock=_Clock())

    def boom() -> None:
        raise RuntimeError("fail")

    # WHEN it fails up to the threshold
    for _ in range(2):
        with pytest.raises(RuntimeError):
            breaker.call(boom)
    # THEN it is open and rejects further calls immediately
    assert breaker.state is CircuitState.OPEN
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "unreached")


def test_breaker_half_opens_after_timeout_then_closes_on_success():
    # GIVEN an open breaker
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=5.0, clock=clock)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state is CircuitState.OPEN
    # WHEN the reset timeout elapses
    clock.t = 5.0
    assert breaker.state is CircuitState.HALF_OPEN
    # THEN a successful trial call closes the breaker
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.state is CircuitState.CLOSED


def test_breaker_reopens_on_half_open_failure():
    # GIVEN a breaker that has half-opened
    clock = _Clock()
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=5.0, clock=clock)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    clock.t = 5.0
    assert breaker.state is CircuitState.HALF_OPEN
    # WHEN the trial call fails THEN the breaker re-opens
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("again")))
    assert breaker.state is CircuitState.OPEN
