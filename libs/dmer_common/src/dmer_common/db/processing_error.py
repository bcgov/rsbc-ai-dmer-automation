"""``processing_error`` -- the failure register.

See docs/development/data-model.md ("processing_error") and
stages/10-dlq-drain.md ("Reason codes"). Every route to
``pipeline_status = MANUAL_REVIEW`` records one row: the stage, the
``failure_category`` that governs what happens next, and a stable
``reason_code``. Never document content -- ``message`` is a fixed description.
"""

from __future__ import annotations

import enum
from datetime import datetime
from typing import Final

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncConnection

from .dmer_stage_run import _stage_enum

metadata = MetaData()


class FailureCategory(str, enum.Enum):
    """``processing_error.failure_category`` (V0006)."""

    PERMANENT_BUSINESS = "PERMANENT_BUSINESS"  # the document itself can't proceed
    TRANSIENT = "TRANSIENT"  # our infrastructure; safe to redrive
    PROCESSING = "PROCESSING"  # a dependency throttled/down; fix, then redrive
    UNKNOWN = "UNKNOWN"  # couldn't classify; human triage


# The legacy error_class each category corresponds to (data-model.md);
# UNKNOWN has none.
_LEGACY_CLASS: Final = {
    FailureCategory.PERMANENT_BUSINESS: "POISON",
    FailureCategory.TRANSIENT: "TRANSIENT",
    FailureCategory.PROCESSING: "DOWNSTREAM",
    FailureCategory.UNKNOWN: None,
}

processing_error = Table(
    "processing_error",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column("stage", _stage_enum, nullable=False),
    Column(
        "error_class",
        PG_ENUM(
            "TRANSIENT",
            "POISON",
            "DOWNSTREAM",
            name="processing_error_class",
            create_type=False,
        ),
    ),
    Column(
        "failure_category",
        PG_ENUM(
            *(c.value for c in FailureCategory),
            name="processing_failure_category",
            create_type=False,
        ),
    ),
    Column("reason_code", Text),
    Column("message", Text),
    Column("dlq_message_id", Text),
    Column("redrive_count", Integer),
    Column("occurred_at", DateTime(timezone=True)),
)


async def record_processing_error(
    conn: AsyncConnection,
    *,
    document_id: str,
    stage: str,
    category: FailureCategory,
    reason_code: str,
    message: str,
    now: datetime,
) -> None:
    """Insert one ``processing_error`` row on *conn* -- inside the caller's
    transaction, so it commits together with the ``MANUAL_REVIEW`` update."""
    category = FailureCategory(category)
    await conn.execute(
        processing_error.insert().values(
            document_id=document_id,
            stage=stage,
            error_class=_LEGACY_CLASS[category],
            failure_category=category.value,
            reason_code=reason_code,
            message=message,
            occurred_at=now,
        )
    )
