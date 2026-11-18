"""``poll_checkpoint`` table repository.

Where the Page Poller got to, per source (``BACKLOG`` or ``REALTIME`` — see
``docs/development/stages/01-ingest.md``). ``last_cursor`` holds Mercury's
``nextLink`` URL verbatim, not a page number — see
``docs/development/data-model.md``'s note on why the architecture doc's
original ``int`` type didn't fit cursor-based pagination.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

poll_checkpoint = Table(
    "poll_checkpoint",
    metadata,
    Column("source", Text, primary_key=True),
    Column("last_cursor", Text, nullable=True),
    Column("last_received_date", DateTime(timezone=True), nullable=True),
    Column("last_run_at", DateTime(timezone=True), nullable=True),
)


@dataclass(frozen=True)
class PollCheckpointRecord:
    """A row in the ``poll_checkpoint`` table."""

    source: str
    last_cursor: str | None = None
    last_received_date: datetime | None = None
    last_run_at: datetime | None = None


class PollCheckpointRepository:
    """Async repository over the ``poll_checkpoint`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get(self, source: str) -> PollCheckpointRecord | None:
        """Return the checkpoint row for ``source``, or ``None`` if it has never run."""
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    poll_checkpoint.c.source,
                    poll_checkpoint.c.last_cursor,
                    poll_checkpoint.c.last_received_date,
                    poll_checkpoint.c.last_run_at,
                ).where(poll_checkpoint.c.source == source)
            )
            row = result.first()
        return PollCheckpointRecord(*row) if row else None

    async def update(
        self,
        source: str,
        *,
        last_cursor: str | None,
        last_received_date: datetime | None,
        last_run_at: datetime,
    ) -> None:
        """Upsert the checkpoint for ``source`` after a poll (first run has no
        existing row, so this is an upsert rather than a plain UPDATE, despite
        the "conceptually always exists after the first run" table purpose).
        """
        values = {
            "source": source,
            "last_cursor": last_cursor,
            "last_received_date": last_received_date,
            "last_run_at": last_run_at,
        }
        stmt = pg_insert(poll_checkpoint).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[poll_checkpoint.c.source],
            set_={k: v for k, v in values.items() if k != "source"},
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
