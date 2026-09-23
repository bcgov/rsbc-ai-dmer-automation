"""``dmer_stage_run`` audit-trail repository (revised architecture).

One row per document, per stage, per attempt — the pipeline's audit trail. **Every
stage writes one of these**: insert ``RUNNING`` at the start, update to
``SUCCEEDED`` / ``FAILED`` at the end (see ``docs/development/data-model.md``
§``dmer_stage_run``). This module provides the shared start/succeed/fail writer so
each stage calls the same convention rather than reimplementing it.

``model_version`` records the DI custom-model version and/or the LLM prompt/schema
version, so a disputed extraction can be traced to the exact model/prompt that
produced it.

``attempt_no`` is derived from the table (previous attempts for the same
document + stage, plus one) rather than from the queue message: Service Bus
redelivery does not change the message's own ``attempt`` field. Starting a new
attempt also closes any row still ``RUNNING`` for that document + stage as
``FAILED`` / ``ABANDONED`` — a run that crashed or lost its lock never reached
its own finish call, and must not look in-flight forever.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    func,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from .status import PipelineStage

metadata = MetaData()

# error_code written on a RUNNING row superseded by a newer attempt.
ABANDONED_ERROR_CODE = "ABANDONED"


class StageRunStatus(str, enum.Enum):
    """Status of a single stage attempt."""

    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


dmer_stage_run = Table(
    "dmer_stage_run",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("document_id", String, nullable=False),
    Column("stage", String, nullable=False),
    Column("status", String, nullable=False),
    Column("attempt_no", Integer, nullable=False, default=1),
    Column("started_at", DateTime(timezone=True), server_default=func.now()),
    Column("ended_at", DateTime(timezone=True), nullable=True),
    Column("output_blob_url", String, nullable=True),
    Column("model_version", String, nullable=True),
    Column("error_code", String, nullable=True),
    Column("error_detail", String, nullable=True),
)


class StageRunRepository:
    """Async repository over the ``dmer_stage_run`` audit table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(
        self,
        document_id: str,
        stage: PipelineStage,
        *,
        model_version: str | None = None,
    ) -> int:
        """Insert a ``RUNNING`` row for the next attempt and return its id.

        In one transaction: close any still-``RUNNING`` row for this document +
        stage as abandoned, then insert with ``attempt_no`` = previous max + 1.
        """
        same_stage = (dmer_stage_run.c.document_id == document_id) & (
            dmer_stage_run.c.stage == stage.value
        )
        abandon = (
            update(dmer_stage_run)
            .where(
                same_stage & (dmer_stage_run.c.status == StageRunStatus.RUNNING.value)
            )
            .values(
                status=StageRunStatus.FAILED.value,
                ended_at=func.now(),
                error_code=ABANDONED_ERROR_CODE,
            )
        )
        last_attempt = select(
            func.coalesce(func.max(dmer_stage_run.c.attempt_no), 0)
        ).where(same_stage)
        async with self._engine.begin() as conn:
            await conn.execute(abandon)
            attempt_no = int((await conn.execute(last_attempt)).scalar_one()) + 1
            result = await conn.execute(
                pg_insert(dmer_stage_run)
                .values(
                    document_id=document_id,
                    stage=stage.value,
                    status=StageRunStatus.RUNNING.value,
                    attempt_no=attempt_no,
                    model_version=model_version,
                )
                .returning(dmer_stage_run.c.id)
            )
            return int(result.scalar_one())

    async def succeed(self, run_id: int, *, output_blob_url: str | None = None) -> None:
        """Mark a stage run ``SUCCEEDED`` with its output blob URL."""
        await self._finish(
            run_id, StageRunStatus.SUCCEEDED, output_blob_url=output_blob_url
        )

    async def fail(
        self, run_id: int, *, error_code: str, error_detail: str | None = None
    ) -> None:
        """Mark a stage run ``FAILED`` with an error code/detail."""
        await self._finish(
            run_id,
            StageRunStatus.FAILED,
            error_code=error_code,
            error_detail=error_detail,
        )

    async def _finish(
        self,
        run_id: int,
        status: StageRunStatus,
        *,
        output_blob_url: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        values: dict[str, object] = {
            "status": status.value,
            "ended_at": func.now(),
        }
        if output_blob_url is not None:
            values["output_blob_url"] = output_blob_url
        if error_code is not None:
            values["error_code"] = error_code
        if error_detail is not None:
            values["error_detail"] = error_detail
        stmt = (
            update(dmer_stage_run).where(dmer_stage_run.c.id == run_id).values(**values)
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
