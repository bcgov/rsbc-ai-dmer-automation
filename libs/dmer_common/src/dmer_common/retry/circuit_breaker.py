"""A minimal in-process circuit breaker.

State machine:

- ``CLOSED``    — calls flow through; consecutive failures are counted.
- ``OPEN``      — calls are rejected immediately with :class:`CircuitOpenError`
                  until ``reset_timeout`` elapses.
- ``HALF_OPEN`` — a single trial call is allowed; success closes the breaker,
                  failure re-opens it.

Deliberately dependency-free and synchronous so the failure/recovery behaviour
is deterministic and unit-testable (a monotonic clock is injectable).
"""

from __future__ import annotations

import enum
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

T = TypeVar("T")


class CircuitState(enum.Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a call is attempted while the breaker is open."""


class CircuitBreaker:
    """Trips open after ``failure_threshold`` consecutive failures.

    Parameters
    ----------
    failure_threshold:
        Consecutive failures that trip the breaker open.
    reset_timeout:
        Seconds the breaker stays open before allowing a half-open trial.
    expected_exceptions:
        Exception types counted as failures; others propagate without tripping.
    clock:
        Monotonic time source (injectable for tests).
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        expected_exceptions: tuple[type[BaseException], ...] = (Exception,),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self._failure_threshold = failure_threshold
        self._reset_timeout = reset_timeout
        self._expected = expected_exceptions
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> CircuitState:
        """Current state, accounting for elapsed reset timeout."""
        with self._lock:
            self._refresh_locked()
            return self._state

    def _refresh_locked(self) -> None:
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self._reset_timeout
        ):
            self._state = CircuitState.HALF_OPEN

    def _before_call(self) -> None:
        with self._lock:
            self._refresh_locked()
            if self._state is CircuitState.OPEN:
                raise CircuitOpenError("circuit breaker is open")

    def _on_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = CircuitState.CLOSED
            self._opened_at = None

    def _on_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if (
                self._state is CircuitState.HALF_OPEN
                or self._failures >= self._failure_threshold
            ):
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    def call(self, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Invoke ``func`` through the breaker, tracking success/failure."""
        self._before_call()
        try:
            result = func(*args, **kwargs)
        except self._expected:
            self._on_failure()
            raise
        else:
            self._on_success()
            return result
