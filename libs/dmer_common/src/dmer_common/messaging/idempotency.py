"""Durable message idempotency: claim -> handle -> complete (or release).

A consumer must not run its handler twice for the same ``messageId``, even across
restarts, multiple replicas, and duplicate Service Bus deliveries. The store makes
that decision **atomically**, in one step, instead of the racy
``is_processed() -> handler -> mark_processed()``:

1. :meth:`IdempotencyStore.claim` atomically records a ``PROCESSING`` claim for
   ``(scope, message_id)`` (a database uniqueness constraint in production).
   Exactly one worker gets :attr:`ClaimOutcome.CLAIMED` and runs the handler.
2. On handler success the claimer calls :meth:`~IdempotencyStore.complete`; the
   record becomes ``COMPLETED`` and later deliveries get
   :attr:`ClaimOutcome.DUPLICATE`.
3. On handler failure the claimer calls :meth:`~IdempotencyStore.release`; the
   claim is removed, so the message stays eligible for retry / dead-letter
   redrive. A message is never marked completed before its handler succeeds.
4. A claim carries a **lease**. If its worker crashes, the claim can be taken
   over once the lease expires; until then other workers get
   :attr:`ClaimOutcome.IN_PROGRESS` and must leave the message for redelivery.

Every record is scoped to one consumer (e.g. ``di-processor/dmer-raw``), so a
store shared between services never lets one consumer's completed ``messageId``
suppress another's.

Guarantee: **at-least-once handling with effectively-once side effects** where
the handler itself is idempotent — not exactly-once. A handler can still run
twice for one message (a crash after the handler but before ``complete``, or a
claim taken over after its lease expired while the original worker was still
running). Handlers must therefore stay idempotent; this store makes duplicate
runs rare and suppresses every duplicate delivery of a completed message.

:class:`InMemoryIdempotencyStore` implements the same rules for unit tests
only. Production uses
:class:`~dmer_common.messaging.postgres_idempotency.PostgresIdempotencyStore`.
"""

from __future__ import annotations

import enum
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

# Default claim lease: matches the Service Bus lock duration on dmer-raw
# (5 minutes), so by the time a message is redelivered after a lost lock, a
# crashed worker's claim is also takeable.
DEFAULT_LEASE_SECONDS: Final = 300


class ClaimOutcome(str, enum.Enum):
    """Result of trying to claim a message."""

    CLAIMED = "CLAIMED"  # this worker owns it: run the handler
    DUPLICATE = "DUPLICATE"  # already completed: settle, don't run the handler
    IN_PROGRESS = "IN_PROGRESS"  # another worker holds a live claim: leave it


@dataclass(frozen=True)
class Claim:
    """A claim attempt's outcome; ``token`` is set only when ``CLAIMED``."""

    outcome: ClaimOutcome
    token: str | None = None


@runtime_checkable
class IdempotencyStore(Protocol):
    """Atomic claim / complete / release of ``(scope, message_id)`` records."""

    def claim(self, scope: str, message_id: str) -> Claim:
        """Atomically claim ``message_id`` in ``scope`` (see module docs)."""
        ...

    def complete(self, scope: str, message_id: str, token: str) -> bool:
        """Mark a claim ``COMPLETED``; False if ``token`` no longer holds it."""
        ...

    def release(self, scope: str, message_id: str, token: str) -> None:
        """Drop a ``PROCESSING`` claim held by ``token`` (handler failed)."""
        ...


@dataclass
class _Record:
    status: str  # "PROCESSING" | "COMPLETED"
    token: str
    lease_expires_at: float


class InMemoryIdempotencyStore:
    """Process-local store with the production rules. **Tests only.**

    Never use it in a deployed service: it forgets everything on restart and is
    invisible to other replicas.
    """

    def __init__(
        self,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._lease = lease_seconds
        self._clock = clock
        self._records: dict[tuple[str, str], _Record] = {}

    def claim(self, scope: str, message_id: str) -> Claim:
        key = (scope, message_id)
        now = self._clock()
        record = self._records.get(key)
        if record is not None:
            if record.status == "COMPLETED":
                return Claim(ClaimOutcome.DUPLICATE)
            if record.lease_expires_at > now:
                return Claim(ClaimOutcome.IN_PROGRESS)
        token = str(uuid.uuid4())  # new claim, or takeover of an expired one
        self._records[key] = _Record("PROCESSING", token, now + self._lease)
        return Claim(ClaimOutcome.CLAIMED, token)

    def complete(self, scope: str, message_id: str, token: str) -> bool:
        record = self._records.get((scope, message_id))
        if record is None or record.token != token:
            return False
        record.status = "COMPLETED"
        return True

    def release(self, scope: str, message_id: str, token: str) -> None:
        key = (scope, message_id)
        record = self._records.get(key)
        if (
            record is not None
            and record.token == token
            and record.status == "PROCESSING"
        ):
            del self._records[key]
