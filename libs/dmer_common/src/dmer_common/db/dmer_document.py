"""``dmer_document`` table repository (revised architecture).

One row per ``document_guid`` (Mercury's identifier). Carries Mercury metadata as
last received plus two denormalized pointers for cheap current-state queries:
``current_stage`` (where the document is) and ``pipeline_status`` (how it is
doing). See ``docs/development/data-model.md`` §``dmer_document``.

``pipeline_status`` changes are validated against the state machine in
:mod:`dmer_common.db.status` before being persisted, so an illegal transition is
rejected in code (and covered by unit tests) rather than silently written. The
pure transition logic (``_validated_status``) is testable without a database.

Scope note: this repository exposes the surface di-processor needs (upsert the
extraction stage's status + the combined-extraction blob URL, read the current
status and stored blob URL for the replay guard). Columns owned by other stages (``mercury_case_id``,
``document_priority``, etc.) are part of the table but not written here.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Column,
    DateTime,
    MetaData,
    String,
    Table,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .status import PipelineStage, PipelineStatus, is_valid_transition

metadata = MetaData()

dmer_document = Table(
    "dmer_document",
    metadata,
    Column("id", String, primary_key=True),
    Column("document_guid", String, nullable=False, unique=True),
    Column("correlation_id", String, nullable=False),
    Column("driver_key", String, nullable=True),
    Column("current_stage", String, nullable=False),
    Column("pipeline_status", String, nullable=False),
    Column("extracted_blob_url", String, nullable=True),
    Column("first_seen_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@dataclass(frozen=True)
class DmerDocumentRecord:
    """A row in the ``dmer_document`` table (di-processor's view)."""

    id: str
    document_guid: str
    correlation_id: str
    current_stage: PipelineStage
    pipeline_status: PipelineStatus
    driver_key: str | None = None
    extracted_blob_url: str | None = None


class InvalidStatusTransition(RuntimeError):
    """Raised when a ``pipeline_status`` update violates the state machine."""


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

    async def get_extracted_blob_url(self, document_id: str) -> str | None:
        """Return the stored combined-extraction blob URL, or None if unset/absent."""
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(dmer_document.c.extracted_blob_url).where(
                    dmer_document.c.id == document_id
                )
            )
            row = result.first()
        return row[0] if row else None

    async def upsert_status(
        self,
        document_id: str,
        correlation_id: str,
        status: PipelineStatus,
        *,
        document_guid: str | None = None,
        stage: PipelineStage | None = None,
        extracted_blob_url: str | None = None,
    ) -> None:
        """Insert or update a document row, validating the status transition.

        The transition is validated against the row's existing status (if any)
        before writing; an illegal transition raises before any SQL executes.
        On the initial insert ``document_guid`` and ``stage`` are required (both
        columns are ``NOT NULL``); on subsequent updates they are optional.

        ``stage`` (``current_stage``) is written only when given — ``None`` leaves
        it unchanged. A stage that finishes passes the *next* stage (Extraction
        sets ``NORMALIZE`` with ``EXTRACTED``); a status-only write (e.g.
        ``MANUAL_REVIEW``) must not move the document's position.

        An existing row is changed with a plain ``UPDATE``, never ``INSERT ... ON
        CONFLICT``: PostgreSQL checks ``NOT NULL`` on the proposed insert row
        before resolving the conflict, so an upsert that omits ``document_guid``
        fails even though the row exists. A plain ``UPDATE`` also applies the
        ``updated_at`` ``onupdate`` (``ON CONFLICT ... SET`` does not), which the
        reconciliation sweeper's stall detection depends on.
        """
        current = await self.get_status(document_id)
        target = _validated_status(current, status)

        values: dict[str, object] = {
            "correlation_id": correlation_id,
            "pipeline_status": target.value,
        }
        if stage is not None:
            values["current_stage"] = stage.value
        if document_guid is not None:
            values["document_guid"] = document_guid
        if extracted_blob_url is not None:
            values["extracted_blob_url"] = extracted_blob_url

        if current is None:
            if not document_guid:
                raise ValueError("document_guid is required on the initial insert")
            if stage is None:
                raise ValueError("stage is required on the initial insert")
            stmt = (
                pg_insert(dmer_document)
                .values(id=document_id, **values)
                .on_conflict_do_update(index_elements=[dmer_document.c.id], set_=values)
            )
        else:
            stmt = (
                update(dmer_document)
                .where(dmer_document.c.id == document_id)
                .values(**values)
            )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
