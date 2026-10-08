"""Document status writes the Document Orchestration makes between activities.

See docs/development/stages/03-document-orchestration.md: after the Rule
Engine the driver is signalled (``RULES_APPLIED`` -> ``AWAITING_DRIVER_COMPLETION``),
and a document an activity could not process is routed to ``MANUAL_REVIEW``
with a ``processing_error`` row. Both are compare-and-set updates, safe to
repeat on retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, Table, select
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine

from .dmer_document import _pipeline_status_enum
from .processing_error import FailureCategory, record_processing_error
from .status import PipelineStatus, next_statuses

metadata = MetaData()

# The dmer_document columns these writes need.
_document = Table(
    "dmer_document",
    metadata,
    Column("id", PG_UUID(as_uuid=False), primary_key=True),
    Column("document_guid", PG_UUID(as_uuid=False)),
    Column("driver_key", PG_UUID(as_uuid=False)),
    Column("pipeline_status", _pipeline_status_enum),
    Column("updated_at", DateTime(timezone=True)),
)

# Statuses a document can be routed to MANUAL_REVIEW from.
_REVIEWABLE = tuple(
    s.value for s in PipelineStatus if PipelineStatus.MANUAL_REVIEW in next_statuses(s)
)


@dataclass(frozen=True)
class DocumentSignalState:
    document_guid: str
    driver_key: str | None
    pipeline_status: str


async def load_signal_state(
    engine: AsyncEngine, document_id: str
) -> DocumentSignalState | None:
    """What the driver-decision publish needs to know about the document."""
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    _document.c.document_guid,
                    _document.c.driver_key,
                    _document.c.pipeline_status,
                ).where(_document.c.id == document_id)
            )
        ).first()
    if row is None:
        return None
    return DocumentSignalState(str(row[0]), str(row[1]) if row[1] else None, row[2])


async def mark_awaiting_driver_completion(
    engine: AsyncEngine, document_id: str, *, now: datetime
) -> bool:
    """``RULES_APPLIED`` -> ``AWAITING_DRIVER_COMPLETION``; False when the
    document wasn't ``RULES_APPLIED`` (already moved on, or not there yet)."""
    async with engine.begin() as conn:
        moved = await conn.execute(
            _document.update()
            .where(
                _document.c.id == document_id,
                _document.c.pipeline_status == PipelineStatus.RULES_APPLIED.value,
            )
            .values(
                pipeline_status=PipelineStatus.AWAITING_DRIVER_COMPLETION.value,
                updated_at=now,
            )
            .returning(_document.c.id)
        )
        return moved.first() is not None


async def route_to_manual_review(
    engine: AsyncEngine,
    document_id: str,
    *,
    stage: str,
    category: FailureCategory,
    reason_code: str,
    message: str,
    now: datetime,
) -> bool:
    """Route the document to ``MANUAL_REVIEW`` and record why in
    ``processing_error``, in one transaction. *reason_code* is a code, never
    document content. False (nothing written) when the document is already
    terminal -- ``MANUAL_REVIEW`` (its first record stands) or ``COMPLETED``."""
    async with engine.begin() as conn:
        routed = await conn.execute(
            _document.update()
            .where(
                _document.c.id == document_id,
                _document.c.pipeline_status.in_(_REVIEWABLE),
            )
            .values(pipeline_status=PipelineStatus.MANUAL_REVIEW.value, updated_at=now)
            .returning(_document.c.id)
        )
        if routed.first() is None:
            return False
        await record_processing_error(
            conn,
            document_id=document_id,
            stage=stage,
            category=category,
            reason_code=reason_code,
            message=message,
            now=now,
        )
        return True
