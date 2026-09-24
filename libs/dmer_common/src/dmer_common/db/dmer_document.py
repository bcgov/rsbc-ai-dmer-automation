"""``dmer_document`` table repository.

One row per ``document_guid`` (Mercury's identifier) -- see
``docs/development/data-model.md``. This supersedes ``documents.py``'s
``documents`` table for the revised architecture's Ingest stage; ``id``
(this row's own primary key) is the tracing/join key used everywhere
downstream, not a separate ``correlation_id`` (see
``docs/development/message-contracts.md``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

# create_type=False on both: the enum types are created by
# database/migrations/V0001__create_dmer_pipeline_schema.sql, not by this
# repository -- SQLAlchemy must not try to (re)create them.
_pipeline_status_enum = PG_ENUM(
    "RECEIVED",
    "DOWNLOADED",
    "EXTRACTING",
    "EXTRACTED",
    "NORMALIZED",
    "RULES_APPLIED",
    "AWAITING_DRIVER_COMPLETION",
    "DECIDED",
    "POSTING",
    "COMPLETED",
    "MANUAL_REVIEW",
    name="dmer_pipeline_status",
    create_type=False,
)
_stage_enum = PG_ENUM(
    "INGEST",
    "EXTRACT",
    "NORMALIZE",
    "RULES",
    "DECISION",
    "POST",
    name="dmer_stage",
    create_type=False,
)

dmer_document = Table(
    "dmer_document",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("document_guid", PG_UUID(as_uuid=False), unique=True, nullable=False),
    Column("document_name", Text, nullable=True),
    Column("mercury_document_status", Text, nullable=True),
    Column("document_priority", Text, nullable=True),
    Column("received_date", DateTime(timezone=True), nullable=True),
    Column("dps_date", DateTime(timezone=True), nullable=True),
    Column("queue", Text, nullable=True),
    Column("business_area", Text, nullable=True),
    Column("mercury_case_id", Text, nullable=True),
    Column("driver_key", PG_UUID(as_uuid=False), nullable=True),
    Column("document_url", Text, nullable=True),
    Column("raw_blob_url", Text, nullable=True),
    Column("pipeline_status", _pipeline_status_enum, nullable=False),
    Column("current_stage", _stage_enum, nullable=False),
    Column("attempt_count", Integer, nullable=False),
    Column("first_seen_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


@dataclass(frozen=True)
class DmerDocumentRecord:
    """A row in the ``dmer_document`` table."""

    id: str
    document_guid: str
    document_name: str | None
    mercury_document_status: str | None
    document_priority: str | None
    received_date: datetime | None
    dps_date: datetime | None
    queue: str | None
    business_area: str | None
    mercury_case_id: str | None
    driver_key: str | None
    document_url: str | None
    raw_blob_url: str | None
    pipeline_status: str
    current_stage: str
    attempt_count: int
    first_seen_at: datetime
    updated_at: datetime


_ALL_COLUMNS = (
    dmer_document.c.id,
    dmer_document.c.document_guid,
    dmer_document.c.document_name,
    dmer_document.c.mercury_document_status,
    dmer_document.c.document_priority,
    dmer_document.c.received_date,
    dmer_document.c.dps_date,
    dmer_document.c.queue,
    dmer_document.c.business_area,
    dmer_document.c.mercury_case_id,
    dmer_document.c.driver_key,
    dmer_document.c.document_url,
    dmer_document.c.raw_blob_url,
    dmer_document.c.pipeline_status,
    dmer_document.c.current_stage,
    dmer_document.c.attempt_count,
    dmer_document.c.first_seen_at,
    dmer_document.c.updated_at,
)


class DmerDocumentRepository:
    """Async repository over the ``dmer_document`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert_received(
        self,
        *,
        document_guid: str,
        document_name: str | None,
        mercury_document_status: str | None,
        document_priority: str | None,
        received_date: datetime | None,
        dps_date: datetime | None,
        queue: str | None,
        business_area: str | None,
        mercury_case_id: str | None,
        driver_key: str | None,
        document_url: str | None,
        now: datetime,
    ) -> str:
        """Upsert on ``document_guid``, returning the row's ``id`` either way.

        ``INSERT ... ON CONFLICT (document_guid) DO NOTHING`` -- the single
        most important idempotency guarantee in the Ingest stage (see
        01-ingest.md): a redelivered/re-polled document must not reset
        ``pipeline_status`` on a row already mid-flight. Because ``DO
        NOTHING`` returns no row on conflict, ``id`` is always re-fetched by
        ``document_guid`` afterward rather than relying on ``RETURNING``.
        """
        stmt = pg_insert(dmer_document).values(
            document_guid=document_guid,
            document_name=document_name,
            mercury_document_status=mercury_document_status,
            document_priority=document_priority,
            received_date=received_date,
            dps_date=dps_date,
            queue=queue,
            business_area=business_area,
            mercury_case_id=mercury_case_id,
            driver_key=driver_key,
            document_url=document_url,
            pipeline_status="RECEIVED",
            current_stage="INGEST",
            attempt_count=0,
            first_seen_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_nothing(
            index_elements=[dmer_document.c.document_guid]
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

        from sqlalchemy import select

        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(dmer_document.c.id).where(
                    dmer_document.c.document_guid == document_guid
                )
            )
            return result.scalar_one()

    async def get_by_id(self, document_id: str) -> DmerDocumentRecord | None:
        """Return the row for ``document_id`` (the internal PK), or ``None``."""
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(*_ALL_COLUMNS).where(dmer_document.c.id == document_id)
            )
            row = result.first()
        return DmerDocumentRecord(*row) if row else None

    async def mark_downloaded(
        self, document_id: str, *, raw_blob_url: str, now: datetime
    ) -> None:
        """Advance a row to ``DOWNLOADED``/``EXTRACT`` after the Ingest
        Function writes the source PDF to ``raw-dmer`` (see 01-ingest.md).
        """
        stmt = (
            dmer_document.update()
            .where(dmer_document.c.id == document_id)
            .values(
                raw_blob_url=raw_blob_url,
                pipeline_status="DOWNLOADED",
                current_stage="EXTRACT",
                attempt_count=dmer_document.c.attempt_count + 1,
                updated_at=now,
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
