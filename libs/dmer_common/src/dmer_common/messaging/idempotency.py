"""Idempotency helper keyed on ``(scope, messageId)``.

Consumers must no-op on a ``messageId`` already processed to completion
(Requirement 3.4). The store abstraction lets the real implementation be backed
by PostgreSQL while unit tests use the in-memory implementation.

Every lookup is **scoped to one consumer** (e.g. ``di-processor/dmer-raw``). A
store shared between services must never let one consumer's completed
``messageId`` suppress another consumer's message: if two events ever carried the
same ID, an unscoped store would silently complete the second one unprocessed.
A durable implementation should key its table on ``(scope, message_id)``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IdempotencyStore(Protocol):
    """Records which ``messageId``s each consumer has processed to completion."""

    def is_processed(self, scope: str, message_id: str) -> bool:
        """Return True if ``message_id`` has already completed in ``scope``."""
        ...

    def mark_processed(self, scope: str, message_id: str) -> None:
        """Record ``message_id`` as completed in ``scope``."""
        ...


class InMemoryIdempotencyStore:
    """A process-local idempotency store (tests / single-process scenarios)."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def is_processed(self, scope: str, message_id: str) -> bool:
        return (scope, message_id) in self._seen

    def mark_processed(self, scope: str, message_id: str) -> None:
        self._seen.add((scope, message_id))
