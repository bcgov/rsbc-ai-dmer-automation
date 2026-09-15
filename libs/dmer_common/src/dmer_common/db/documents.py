"""``documents`` table repository.

Records a document's processing status and the blob URLs produced along the way
(``initial_extraction_uri``, ``combined_extraction_uri``). Status changes are
validated against the state machine in :mod:`dmer_common.db.status` before being
persisted, so an illegal transition is rejected in code (and covered by unit
tests) rather than silently written.

The SQL layer uses SQLAlchemy Core with an async engine. The async driver (e.g.
``asyncpg``) is only required at engine-creation time, so the pure transition
logic (``_validated_status``) is testable without a database.
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
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .status import DocumentStatus, is_valid_transition

metadata = MetaData()

documents = Table(
    "documents",
    metadata,
    Column("document_id", String, primary_key=True),
    Column("correlation_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("initial_extraction_uri", String, nullable=True),
    Column("combined_extraction_uri", String, nullable=True),
    Column("failure_reason", String, nullable=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column(
        "updated_at",
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    ),
)


@dataclass(frozen=True)
class DocumentRecord:
    """A row in the ``documents`` table."""

    document_id: str
    correlation_id: str
    status: DocumentStatus
    initial_extraction_uri: str | None = None
    combined_extraction_uri: str | None = None
    failure_reason: str | None = None


class InvalidStatusTransition(RuntimeError):
    """Raised when a status update violates the state machine."""


def _validated_status(
    current: DocumentStatus | None, target: DocumentStatus
) -> DocumentStatus:
    """Return ``target`` if the transition from ``current`` is allowed.

    A ``None`` current status represents an initial insert (only ``received`` is
    valid). Raises :class:`InvalidStatusTransition` otherwise.
    """
    if current is None:
        if target is not DocumentStatus.RECEIVED:
            raise InvalidStatusTransition(
                f"initial status must be 'received', not {target.value!r}"
            )
        return target
    if current == target:
        return target
    if not is_valid_transition(current, target):
        raise InvalidStatusTransition(
            f"illegal transition {current.value!r} -> {target.value!r}"
        )
    return target


class DocumentRepository:
    """Async repository over the ``documents`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def get_status(self, document_id: str) -> DocumentStatus | None:
        """Return the current status for a document, or None if it has no row."""
        from sqlalchemy import select

        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(documents.c.status).where(documents.c.document_id == document_id)
            )
            row = result.first()
        return DocumentStatus(row[0]) if row else None

    async def upsert_status(
        self,
        document_id: str,
        correlation_id: str,
        status: DocumentStatus,
        *,
        initial_extraction_uri: str | None = None,
        combined_extraction_uri: str | None = None,
        failure_reason: str | None = None,
    ) -> None:
        """Insert or update a document row, validating the status transition.

        The transition is validated against the row's existing status (if any)
        before writing; an illegal transition raises before any SQL executes.
        """
        current = await self.get_status(document_id)
        target = _validated_status(current, status)

        values: dict[str, object] = {
            "document_id": document_id,
            "correlation_id": correlation_id,
            "status": target.value,
        }
        if initial_extraction_uri is not None:
            values["initial_extraction_uri"] = initial_extraction_uri
        if combined_extraction_uri is not None:
            values["combined_extraction_uri"] = combined_extraction_uri
        if failure_reason is not None:
            values["failure_reason"] = failure_reason

        update_cols = {k: v for k, v in values.items() if k != "document_id"}
        stmt = pg_insert(documents).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[documents.c.document_id], set_=update_cols
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
