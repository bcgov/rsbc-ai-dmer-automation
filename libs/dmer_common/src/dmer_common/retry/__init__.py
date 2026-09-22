"""Standardized retry + circuit-breaker policies for external dependencies.

External calls (Document Intelligence, Azure OpenAI, Service Bus, Mercury) must
be wrapped so transient failures retry with exponential backoff and sustained
outages trip a circuit breaker (fail fast rather than cascade).

Two building blocks are exposed:

- :func:`retry_policy` — a Tenacity ``Retrying``/decorator factory with sensible
  exponential-backoff defaults.
- :class:`CircuitBreaker` — a minimal in-process breaker (closed → open →
  half-open) that raises :class:`CircuitOpenError` while open.

Convenience factories :func:`di_retry`/:func:`di_breaker`,
:func:`openai_retry`/:func:`openai_breaker`, and
:func:`mercury_retry`/:func:`mercury_breaker` capture the tuned policies used
by the di-processor design (Requirements 4.5, 6.6) and the Ingest stage's
Mercury batch API calls (``docs/development/stages/01-ingest.md``).
"""

from __future__ import annotations

from .circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from .policies import (
    di_breaker,
    di_retry,
    mercury_breaker,
    mercury_retry,
    openai_breaker,
    openai_retry,
    retry_policy,
)

__all__ = [
    "CircuitBreaker",
    "CircuitOpenError",
    "CircuitState",
    "di_breaker",
    "di_retry",
    "mercury_breaker",
    "mercury_retry",
    "openai_breaker",
    "openai_retry",
    "retry_policy",
]
