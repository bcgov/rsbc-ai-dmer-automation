"""Service Bus messaging: envelope-aware consumer and publisher.

Every consumer in the pipeline must:
- propagate ``documentId`` from the message into logs and downstream messages,
- be **idempotent** on ``messageId`` via a durable, atomic claim in an
  :class:`IdempotencyStore` (no-op, not error, on an already-completed id),
- ``complete`` on success and ``dead_letter`` on unrecoverable failure.

Every publisher emits the standard envelope (``messageId``/``documentId``/
``schemaVersion``) on the wire in camelCase.

The Azure Service Bus SDK is wrapped (never opened raw in a service). The wrapper
objects are injectable so the send/receive/settlement logic is unit-testable with
a fake bus, and idempotency is pluggable via :class:`IdempotencyStore`.
"""

from __future__ import annotations

from .consumer import ServiceBusConsumer
from .idempotency import (
    DEFAULT_LEASE_SECONDS,
    Claim,
    ClaimOutcome,
    IdempotencyStore,
    InMemoryIdempotencyStore,
)
from .postgres_idempotency import PostgresIdempotencyStore
from .publisher import ServiceBusPublisher

__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "Claim",
    "ClaimOutcome",
    "IdempotencyStore",
    "InMemoryIdempotencyStore",
    "PostgresIdempotencyStore",
    "ServiceBusConsumer",
    "ServiceBusPublisher",
]
