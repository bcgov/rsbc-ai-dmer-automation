"""``dmer_stage_run`` table repository -- the audit trail.

One row per document, per stage, per attempt (see
``docs/development/data-model.md``). Every stage inserts one of these at
the start of its work (``RUNNING``) and updates it to ``SUCCEEDED``/
``FAILED`` at the end -- this repository provides that start/succeed/fail
helper so each stage doesn't reimplement it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

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


class DmerStageRunRepository:
    """Async repository over the ``dmer_stage_run`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def start(
        self, *, document_id: str, stage: str, attempt_no: int, started_at: datetime
    ) -> int:
        """Insert a ``RUNNING`` row, returning its ``id`` for the matching
        :meth:`succeed`/:meth:`fail` call.
        """
        stmt = (
            dmer_stage_run.insert()
            .values(
                document_id=document_id,
                stage=stage,
                status="RUNNING",
                attempt_no=attempt_no,
                started_at=started_at,
            )
            .returning(dmer_stage_run.c.id)
        )
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalar_one()

    async def succeed(
        self, run_id: int, *, ended_at: datetime, output_blob_url: str | None = None
    ) -> None:
        """Mark a run ``SUCCEEDED``."""
        stmt = (
            dmer_stage_run.update()
            .where(dmer_stage_run.c.id == run_id)
            .values(
                status="SUCCEEDED", ended_at=ended_at, output_blob_url=output_blob_url
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)

    async def fail(
        self,
        run_id: int,
        *,
        ended_at: datetime,
        error_code: str | None,
        error_detail: str | None,
    ) -> None:
        """Mark a run ``FAILED`` with an error code/detail."""
        stmt = (
            dmer_stage_run.update()
            .where(dmer_stage_run.c.id == run_id)
            .values(
                status="FAILED",
                ended_at=ended_at,
                error_code=error_code,
                error_detail=error_detail,
            )
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
