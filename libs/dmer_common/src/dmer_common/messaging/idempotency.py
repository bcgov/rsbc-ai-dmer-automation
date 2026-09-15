"""Idempotency helper keyed on ``messageId``.

Consumers must no-op on a ``messageId`` already processed to completion
(Requirement 3.4). The store abstraction lets the real implementation be backed
by PostgreSQL (the ``documents`` table records completion) while unit tests use
the in-memory implementation.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IdempotencyStore(Protocol):
    """Records which ``messageId``s have been processed to completion."""

    def is_processed(self, message_id: str) -> bool:
        """Return True if ``message_id`` has already completed."""
        ...

    def mark_processed(self, message_id: str) -> None:
        """Record ``message_id`` as completed."""
        ...


class InMemoryIdempotencyStore:
    """A process-local idempotency store (tests / single-process scenarios)."""

    def __init__(self) -> None:
        self._seen: set[str] = set()

    def is_processed(self, message_id: str) -> bool:
        return message_id in self._seen

    def mark_processed(self, message_id: str) -> None:
        self._seen.add(message_id)
