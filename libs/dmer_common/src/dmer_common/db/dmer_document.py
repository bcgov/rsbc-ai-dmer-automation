"""``dmer_document`` table repository (revised architecture).

One row per ``document_guid`` (Mercury's identifier). Carries Mercury metadata as
last received plus two denormalized pointers for cheap current-state queries:
``current_stage`` (where the document is) and ``pipeline_status`` (how it is
doing). See ``docs/development/data-model.md`` §``dmer_document``.

``pipeline_status`` changes are **atomic compare-and-set** writes. The caller
passes the status it ``expected`` the row to be in; the write succeeds only if the
row is still in that status (``UPDATE ... WHERE id = :id AND pipeline_status =
:expected``), and the ``expected -> target`` move is validated against the state
machine in :mod:`dmer_common.db.status` first. Two workers on the same document
(e.g. a Service Bus redelivery while the first is still running) therefore cannot
move a status backwards or overwrite each other: the loser gets
:class:`StaleStatusError` and must stop. The pure transition logic
(``_validated_status``) is testable without a database.

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
        expected: PipelineStatus | None,
        document_guid: str | None = None,
        stage: PipelineStage | None = None,
        extracted_blob_url: str | None = None,
    ) -> None:
        """Atomically move a document from ``expected`` to ``status``.

        Compare-and-set: the write applies only if the row is still in
        ``expected``; otherwise :class:`StaleStatusError` is raised and nothing is
        written. ``expected=None`` means "the row must not exist yet" (the
        initial insert). ``expected -> status`` is validated against the state
        machine first, so an illegal move raises :class:`InvalidStatusTransition`
        before any SQL runs. ``expected == status`` is an idempotent re-entry
        (e.g. ``EXTRACTING`` again on redelivery) and succeeds while the row is
        still there.

        On the initial insert ``document_guid`` and ``stage`` are required (both
        columns are ``NOT NULL``); on subsequent updates they are optional.
        ``stage`` (``current_stage``) is written only when given, so a
        status-only write such as ``MANUAL_REVIEW`` leaves the position alone.

        An existing row is changed with a plain ``UPDATE`` (which also applies
        the ``updated_at`` ``onupdate`` the reconciliation sweeper relies on),
        never ``INSERT ... ON CONFLICT ... DO UPDATE``.
        """
        target = _validated_status(expected, status)

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

        if expected is None:
            if not document_guid:
                raise ValueError("document_guid is required on the initial insert")
            if stage is None:
                raise ValueError("stage is required on the initial insert")
            # Conflict only on id: a clash on document_guid (a different id for
            # the same Mercury document) is a real error and still raises.
            stmt = (
                pg_insert(dmer_document)
                .values(id=document_id, **values)
                .on_conflict_do_nothing(index_elements=[dmer_document.c.id])
                .returning(dmer_document.c.id)
            )
        else:
            stmt = (
                update(dmer_document)
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
