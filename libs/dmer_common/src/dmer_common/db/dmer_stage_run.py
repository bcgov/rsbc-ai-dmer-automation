"""``dmer_stage_run`` table repository -- the audit trail.

One row per document, per stage, per attempt (see
``docs/development/data-model.md``). Every stage inserts one of these at
the start of its work (``RUNNING``) and updates it to ``SUCCEEDED``/
``FAILED`` at the end -- this repository provides that start/succeed/fail
helper so each stage doesn't reimplement it. The table is created by
``database/migrations/V0001__create_dmer_pipeline_schema.sql``.

``attempt_no`` may be passed by the caller (Ingest derives it from
``dmer_document.attempt_count``) or left out, in which case it is derived from
the table (previous attempts for the same document + stage, plus one) — the
queue message's own ``attempt`` doesn't change on Service Bus redelivery.
Starting a new attempt also closes any row still ``RUNNING`` for that document +
stage as ``FAILED`` / ``ABANDONED``: a run that crashed or lost its lock never
reached its own finish call and must not look in-flight forever.

``model_version`` records the DI custom-model version and/or the LLM
prompt/schema version, so a disputed extraction can be traced to the exact
model/prompt that produced it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

# error_code written on a RUNNING row superseded by a newer attempt.
ABANDONED_ERROR_CODE: Final = "ABANDONED"


class StageRunStatus(str, enum.Enum):
    """Status of a single stage attempt."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


# create_type=False: created by V0001__create_dmer_pipeline_schema.sql.
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
_status_enum = PG_ENUM(
    "RUNNING", "SUCCEEDED", "FAILED", name="dmer_stage_run_status", create_type=False
)

dmer_stage_run = Table(
    "dmer_stage_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column("stage", _stage_enum, nullable=False),
    Column("status", _status_enum, nullable=False),
    Column("attempt_no", Integer, nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("output_blob_url", Text, nullable=True),
    Column("model_version", Text, nullable=True),
    Column("error_code", Text, nullable=True),
    Column("error_detail", Text, nullable=True),
)


@dataclass(frozen=True)
class DmerStageRunRecord:
    """A row in the ``dmer_stage_run`` table."""

    id: int
    document_id: str
    stage: str
    status: str
    attempt_no: int
    started_at: datetime | None
    ended_at: datetime | None
    output_blob_url: str | None
    model_version: str | None
    error_code: str | None
    error_detail: str | None


def _value(v: object) -> object:
    """Accept an enum member or its string value."""
    return v.value if isinstance(v, enum.Enum) else v


class DmerStageRunRepository:
    """Async repository over the ``dmer_stage_run`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(
        self,
        *,
        document_id: str,
        stage: str | enum.Enum,
        attempt_no: int | None = None,
        started_at: datetime | None = None,
        model_version: str | None = None,
    ) -> int:
        """Insert a ``RUNNING`` row, returning its ``id`` for the matching
        :meth:`succeed`/:meth:`fail` call.

        In one transaction: close any still-``RUNNING`` row for this document +
        stage as abandoned, then insert. ``attempt_no`` defaults to the previous
        max + 1; ``started_at`` defaults to the database clock.
        """
        stage_value = _value(stage)
        same_stage = (dmer_stage_run.c.document_id == document_id) & (
            dmer_stage_run.c.stage == stage_value
        )
        abandon = (
            dmer_stage_run.update()
            .where(
                same_stage & (dmer_stage_run.c.status == StageRunStatus.RUNNING.value)
            )
            .values(
                status=StageRunStatus.FAILED.value,
                ended_at=func.now(),
                error_code=ABANDONED_ERROR_CODE,
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(abandon)
            if attempt_no is None:
                last = (
                    await conn.execute(
                        select(
                            func.coalesce(func.max(dmer_stage_run.c.attempt_no), 0)
                        ).where(same_stage)
                    )
                ).scalar_one()
                attempt_no = int(last) + 1
            result = await conn.execute(
                dmer_stage_run.insert()
                .values(
                    document_id=document_id,
                    stage=stage_value,
                    status=StageRunStatus.RUNNING.value,
                    attempt_no=attempt_no,
                    started_at=started_at if started_at is not None else func.now(),
                    model_version=model_version,
                )
                .returning(dmer_stage_run.c.id)
            )
            return result.scalar_one()

    async def succeed(
        self,
        run_id: int,
        *,
        ended_at: datetime | None = None,
        output_blob_url: str | None = None,
    ) -> None:
        """Mark a run ``SUCCEEDED``."""
        stmt = (
            dmer_stage_run.update()
            .where(dmer_stage_run.c.id == run_id)
            .values(
                status=StageRunStatus.SUCCEEDED.value,
                ended_at=ended_at if ended_at is not None else func.now(),
                output_blob_url=output_blob_url,
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def fail(
        self,
        run_id: int,
        *,
        ended_at: datetime | None = None,
        error_code: str | None,
        error_detail: str | None = None,
    ) -> None:
        """Mark a run ``FAILED`` with an error code/detail."""
        stmt = (
            dmer_stage_run.update()
            .where(dmer_stage_run.c.id == run_id)
            .values(
                status=StageRunStatus.FAILED.value,
                ended_at=ended_at if ended_at is not None else func.now(),
                error_code=error_code,
                error_detail=error_detail,
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
