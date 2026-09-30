"""``dmer_document`` table repository.

One row per ``document_guid`` (Mercury's identifier) -- see
``docs/development/data-model.md``. The table itself is created by
``database/migrations/V0001__create_dmer_pipeline_schema.sql``; this module only
mirrors it. ``id`` (this row's own primary key) is the tracing/join key used
everywhere downstream, not a separate ``correlation_id`` (see
``docs/development/message-contracts.md``).

Two groups of writes:

- **Ingest** (``upsert_received``, ``mark_downloaded``): creates the row and
  advances it to ``DOWNLOADED``.
- **Later stages** (``upsert_status``): **atomic compare-and-set** status
  writes. The caller passes the status it ``expected`` the row to be in; the
  write succeeds only if the row is still in that status (``UPDATE ... WHERE id
  = :id AND pipeline_status = :expected``), and the ``expected -> target`` move
  is validated against the state machine in :mod:`dmer_common.db.status`
  first. Two workers on the same document (e.g. a Service Bus redelivery while
  the first is still running) therefore cannot move a status backwards or
  overwrite each other: the loser gets :class:`StaleStatusError` and must stop.
  The pure transition logic (``_validated_status``) is testable without a
  database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, Text, func, select
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .status import PipelineStage, PipelineStatus, is_valid_transition

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


class InvalidStatusTransition(RuntimeError):
    """Raised when a ``pipeline_status`` update violates the state machine."""


class StaleStatusError(RuntimeError):
    """Raised when the row is no longer in the status the caller expected.

    Another worker changed (or created) the row between the caller's read and its
    write. The caller has lost the race and must stop without overwriting the
    winner's result. ``actual`` is the status found afterwards (``None`` if the
    row is absent).
    """

    def __init__(
        self,
        document_id: str,
        expected: PipelineStatus | None,
        actual: PipelineStatus | None,
    ) -> None:
        self.document_id = document_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"document {document_id}: expected status "
            f"{expected.value if expected else None!r}, "
            f"found {actual.value if actual else None!r}"
        )


def _validated_status(
    current: PipelineStatus | None, target: PipelineStatus
) -> PipelineStatus:
    """Return ``target`` if the transition from ``current`` is allowed.

    A ``None`` current status represents an initial insert (only ``RECEIVED`` is
    valid). ``current == target`` is an idempotent no-op. Raises
    :class:`InvalidStatusTransition` otherwise.
    """
    if current is None:
        if target is not PipelineStatus.RECEIVED:
            raise InvalidStatusTransition(
                f"initial status must be 'RECEIVED', not {target.value!r}"
            )
        return target
    if current == target:
        return target
    if not is_valid_transition(current, target):
        raise InvalidStatusTransition(
            f"illegal transition {current.value!r} -> {target.value!r}"
        )
    return target


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

        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(dmer_document.c.id).where(
                    dmer_document.c.document_guid == document_guid
                )
            )
            return result.scalar_one()

    async def get_by_id(self, document_id: str) -> DmerDocumentRecord | None:
        """Return the row for ``document_id`` (the internal PK), or ``None``."""
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

    async def get_status(self, document_id: str) -> PipelineStatus | None:
        """Return the current ``pipeline_status``, or None if the row is absent."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(dmer_document.c.pipeline_status).where(
                    dmer_document.c.id == document_id
                )
            )
            row = result.first()
        return PipelineStatus(row[0]) if row else None

    async def upsert_status(
        self,
        document_id: str,
        status: PipelineStatus,
        *,
        expected: PipelineStatus | None,
        document_guid: str | None = None,
        stage: PipelineStage | None = None,
    ) -> None:
        """Atomically move a document from ``expected`` to ``status``.

        Compare-and-set: the write applies only if the row is still in
        ``expected``; otherwise :class:`StaleStatusError` is raised and nothing is
        written. ``expected=None`` means "the row must not exist yet" (a
        bootstrap insert when no Ingest row exists). ``expected -> status`` is
        validated against the state machine first, so an illegal move raises
        :class:`InvalidStatusTransition` before any SQL runs. ``expected ==
        status`` is an idempotent re-entry (e.g. ``EXTRACTING`` again on
        redelivery) and succeeds while the row is still there.

        On the insert ``document_guid`` and ``stage`` are required; on updates
        they are optional. ``stage`` (``current_stage``) is written only when
        given, so a status-only write such as ``MANUAL_REVIEW`` leaves the
        position alone. Every write sets ``updated_at`` (the reconciliation
        sweeper's stall detection scans it).
        """
        target = _validated_status(expected, status)
        now = func.now()

        values: dict[str, object] = {
            "pipeline_status": target.value,
            "updated_at": now,
        }
        if stage is not None:
            values["current_stage"] = stage.value
        if document_guid is not None:
            values["document_guid"] = document_guid

        if expected is None:
            if not document_guid:
                raise ValueError("document_guid is required on the initial insert")
            if stage is None:
                raise ValueError("stage is required on the initial insert")
            # Conflict only on id: a clash on document_guid (a different id for
            # the same Mercury document) is a real error and still raises.
            stmt = (
                pg_insert(dmer_document)
                .values(
                    id=document_id,
                    attempt_count=0,
                    first_seen_at=now,
                    **values,
                )
                .on_conflict_do_nothing(index_elements=[dmer_document.c.id])
                .returning(dmer_document.c.id)
            )
        else:
            stmt = (
                dmer_document.update()
                .where(
                    (dmer_document.c.id == document_id)
                    & (dmer_document.c.pipeline_status == expected.value)
                )
                .values(**values)
                .returning(dmer_document.c.id)
            )
        async with self._engine.begin() as conn:
            applied = (await conn.execute(stmt)).first() is not None
        if not applied:
            raise StaleStatusError(
                document_id, expected, await self.get_status(document_id)
            )
