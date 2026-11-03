"""Tenacity retry-policy factories and tuned per-dependency policies.

``retry_policy`` returns a Tenacity ``retry`` decorator; the DI/OpenAI helpers
capture the backoff/attempt counts referenced by the di-processor design
(Requirements 4.5, 6.6) so services do not re-invent them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .circuit_breaker import CircuitBreaker


def retry_policy(
    *,
    max_attempts: int = 3,
    initial_wait: float = 0.5,
    max_wait: float = 8.0,
    multiplier: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
    reraise: bool = True,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Build a Tenacity retry decorator with exponential backoff.

    Retries up to ``max_attempts`` times on the given exception types, waiting
    ``initial_wait * multiplier**n`` seconds (capped at ``max_wait``) between
    attempts, then re-raises the final error.
    """
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(
            multiplier=initial_wait, exp_base=multiplier, max=max_wait
        ),
        retry=retry_if_exception_type(retry_on),
        reraise=reraise,
    )


def di_retry(
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry policy tuned for Document Intelligence calls (Req 4.5)."""
    return retry_policy(
        max_attempts=4, initial_wait=1.0, max_wait=15.0, retry_on=retry_on
    )


def openai_retry(
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry policy tuned for external Azure OpenAI calls (Req 6.6)."""
    return retry_policy(
        max_attempts=4, initial_wait=1.0, max_wait=20.0, retry_on=retry_on
    )


def di_breaker() -> CircuitBreaker:
    """Circuit breaker tuned for Document Intelligence."""
    return CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


def openai_breaker() -> CircuitBreaker:
    """Circuit breaker tuned for external Azure OpenAI."""
    return CircuitBreaker(failure_threshold=5, reset_timeout=30.0)


def mercury_retry(
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Retry policy tuned for Mercury batch API calls (see 01-ingest.md's
    "Failure handling": a Mercury call failure is transient, retried inside
    the function with backoff before letting the timer retry the whole page).
    """
    return retry_policy(
        max_attempts=4, initial_wait=1.0, max_wait=20.0, retry_on=retry_on
    )


def mercury_breaker() -> CircuitBreaker:
    """Circuit breaker tuned for the Mercury batch API."""
    return CircuitBreaker(failure_threshold=5, reset_timeout=30.0)
