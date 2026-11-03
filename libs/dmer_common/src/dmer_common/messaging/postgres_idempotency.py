"""PostgreSQL-backed :class:`~dmer_common.messaging.idempotency.IdempotencyStore`.

Table ``message_idempotency``, primary key ``(scope, message_id)``::

    scope             text          consumer name, e.g. "di-processor/dmer-raw"
    message_id        text          Service Bus messageId
    status            text          PROCESSING | COMPLETED
    claim_token       text          random per claim; only its holder may
                                    complete or release it
    claimed_at        timestamptz
    lease_expires_at  timestamptz   a PROCESSING claim past this may be taken over
    processed_at      timestamptz   set when COMPLETED
    attempts          integer       number of claims (diagnostics)

The claim is **one atomic statement** — the primary key is the arbiter::

    INSERT ... VALUES (..., 'PROCESSING', :token, now(), now() + :lease, 1)
    ON CONFLICT (scope, message_id) DO UPDATE
       SET <new claim>, attempts = attempts + 1
     WHERE status = 'PROCESSING' AND lease_expires_at < now()
    RETURNING claim_token

A returned token means this worker claimed it (a fresh claim or a takeover of an
expired one). No row means another record won: ``COMPLETED`` (a duplicate) or a
live ``PROCESSING`` claim (in progress elsewhere). Lease times use the database
clock, so replica clock skew doesn't matter. See
:mod:`dmer_common.messaging.idempotency` for the full semantics and crash
behaviour.

The Service Bus consumer is synchronous, so each call runs its own
``asyncio.run``. The engine must therefore use ``NullPool``: pooled asyncpg
connections are bound to the event loop that created them and fail when reused
from the next ``asyncio.run``. The store's first call is serialized: several
threads making an engine's very first connection at once (its one-time dialect
initialization) can deadlock under per-call event loops; after that, calls run
concurrently.
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from collections.abc import Coroutine
from datetime import timedelta
from typing import Any, TypeVar

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    delete,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from .idempotency import DEFAULT_LEASE_SECONDS, Claim, ClaimOutcome

metadata = MetaData()

message_idempotency = Table(
    "message_idempotency",
    metadata,
    Column("scope", String, nullable=False),
    Column("message_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("claim_token", String, nullable=False),
    Column("claimed_at", DateTime(timezone=True), nullable=False),
    Column("lease_expires_at", DateTime(timezone=True), nullable=False),
    Column("processed_at", DateTime(timezone=True), nullable=True),
    Column("attempts", Integer, nullable=False),
    PrimaryKeyConstraint("scope", "message_id", name="pk_message_idempotency"),
)

_PROCESSING = "PROCESSING"
_COMPLETED = "COMPLETED"

_T = TypeVar("_T")


class PostgresIdempotencyStore:
    """Durable, multi-replica idempotency store over ``message_idempotency``."""

    def __init__(
        self, engine: AsyncEngine, *, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> None:
        if not isinstance(engine.sync_engine.pool, NullPool):
            raise TypeError(
                "PostgresIdempotencyStore needs an engine created with "
                "poolclass=NullPool (each call runs in its own event loop)"
            )
        self._engine = engine
        self._lease = timedelta(seconds=lease_seconds)
        self._warm = False
        self._warm_lock = threading.Lock()

    # --- IdempotencyStore -----------------------------------------------------

    def claim(self, scope: str, message_id: str) -> Claim:
        return self._run(self._claim(scope, message_id))

    def complete(self, scope: str, message_id: str, token: str) -> bool:
        return self._run(self._complete(scope, message_id, token))

    def release(self, scope: str, message_id: str, token: str) -> None:
        self._run(self._release(scope, message_id, token))

    def _run(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run one operation in its own event loop (see module docs).

        The first call is made under a lock so the engine's one-time connection
        initialization never happens on several threads at once.
        """
        if self._warm:
            return asyncio.run(coro)
        with self._warm_lock:
            result = asyncio.run(coro)
            self._warm = True
            return result

    # --- implementation ---------------------------------------------------------

    async def _claim(self, scope: str, message_id: str) -> Claim:
        t = message_idempotency.c
        token = str(uuid.uuid4())
        now = func.now()
        insert = pg_insert(message_idempotency).values(
            scope=scope,
            message_id=message_id,
            status=_PROCESSING,
            claim_token=token,
            claimed_at=now,
            lease_expires_at=now + self._lease,
            attempts=1,
        )
        stmt = insert.on_conflict_do_update(
            index_elements=[t.scope, t.message_id],
            set_={
                "status": _PROCESSING,
                "claim_token": token,
                "claimed_at": now,
                "lease_expires_at": now + self._lease,
                "attempts": t.attempts + 1,
            },
            # Only an expired PROCESSING claim may be taken over; a COMPLETED
            # record or a live claim leaves the row untouched (no row returned).
            where=(t.status == _PROCESSING) & (t.lease_expires_at < now),
        ).returning(t.claim_token)
        async with self._engine.begin() as conn:
            claimed = (await conn.execute(stmt)).first()
            if claimed is not None:
                return Claim(ClaimOutcome.CLAIMED, claimed[0])
            status = (
                await conn.execute(
                    select(t.status).where(
                        (t.scope == scope) & (t.message_id == message_id)
                    )
                )
            ).scalar_one_or_none()
        if status == _COMPLETED:
            return Claim(ClaimOutcome.DUPLICATE)
        # A live claim held elsewhere (or one released an instant ago): leave the
        # message for redelivery rather than risk running it twice.
        return Claim(ClaimOutcome.IN_PROGRESS)

    async def _complete(self, scope: str, message_id: str, token: str) -> bool:
        t = message_idempotency.c
        stmt = (
            update(message_idempotency)
            .where(
                (t.scope == scope)
                & (t.message_id == message_id)
                & (t.claim_token == token)
            )
            .values(status=_COMPLETED, processed_at=func.now())
            .returning(t.message_id)
        )
        async with self._engine.begin() as conn:
            return (await conn.execute(stmt)).first() is not None

    async def _release(self, scope: str, message_id: str, token: str) -> None:
        t = message_idempotency.c
        stmt = delete(message_idempotency).where(
            (t.scope == scope)
            & (t.message_id == message_id)
            & (t.claim_token == token)
            & (t.status == _PROCESSING)
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
