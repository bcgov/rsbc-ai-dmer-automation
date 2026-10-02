"""DB unit-of-work for the Normalize activity.

This module keeps per-document Normalize writes on one dedicated DB session
(single physical connection + advisory lock) so activity retries/replays do not
interleave or partially apply stage/document updates.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ._advisory import advisory_lock_session
from .dmer_document import dmer_document
from .dmer_stage_run import dmer_stage_run


class NormalizationBusyError(RuntimeError):
    """Raised when another Normalize session already holds the document lock."""

    def __init__(self) -> None:
        super().__init__("Normalization is already running for this document.")


class NormalizationNotReadyError(RuntimeError):
    """Raised when Normalize is attempted before the document reaches EXTRACTED."""

    def __init__(self) -> None:
        super().__init__("Normalization is not ready for this document.")


class NormalizationStateError(RuntimeError):
    """Raised for inconsistent/missing/manual-review Normalize state."""

    def __init__(self) -> None:
        super().__init__("Normalization state is invalid for this operation.")


@dataclass(frozen=True)
class NormalizationRun:
    run_id: int
    attempt_no: int
    output_blob_url: str | None


_NORMALIZE_STAGE = "NORMALIZE"
_RULES_STAGE = "RULES"
_RUNNING = "RUNNING"
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"

_READY_TO_START = {"EXTRACTED", "NORMALIZED"}
_NOT_READY = {"RECEIVED", "DOWNLOADED", "EXTRACTING"}
_REPLAY_ALLOWED = {
    "NORMALIZED",
    "RULES_APPLIED",
    "AWAITING_DRIVER_COMPLETION",
    "DECIDED",
    "POSTING",
    "COMPLETED",
}


class NormalizationRepository:
    """Normalization DB operations bound to one dedicated async connection."""

    def __init__(self, conn: AsyncConnection, *, document_id: str) -> None:
        self._conn = conn
        self._document_id = document_id

    def _check_session(self) -> None:
        # SQLAlchemy can transparently reconnect an invalidated connection.
        # A replacement physical session would not own our advisory lock.
        if self._conn.closed or self._conn.invalidated:
            raise NormalizationBusyError()

    async def start_or_resume(
        self, *, now: datetime, model_version: str
    ) -> NormalizationRun:
        """Start a new Normalize run, or return a persisted successful replay."""
        self._check_session()
        async with self._conn.begin():
            doc_row = (
                await self._conn.execute(
                    select(dmer_document.c.pipeline_status)
                    .where(dmer_document.c.id == self._document_id)
                    .with_for_update()
                )
            ).first()
            if doc_row is None:
                raise NormalizationStateError()

            pipeline_status = doc_row[0]

            if pipeline_status in _NOT_READY:
                raise NormalizationNotReadyError()
            if pipeline_status == "MANUAL_REVIEW":
                raise NormalizationStateError()
            if pipeline_status not in (_READY_TO_START | _REPLAY_ALLOWED):
                raise NormalizationStateError()

            if pipeline_status in _REPLAY_ALLOWED:
                success_row = (
                    await self._conn.execute(
                        select(
                            dmer_stage_run.c.id,
                            dmer_stage_run.c.attempt_no,
                            dmer_stage_run.c.output_blob_url,
                        )
                        .where(
                            dmer_stage_run.c.document_id == self._document_id,
                            dmer_stage_run.c.stage == _NORMALIZE_STAGE,
                            dmer_stage_run.c.status == _SUCCEEDED,
                        )
                        .order_by(
                            dmer_stage_run.c.attempt_no.desc(),
                            dmer_stage_run.c.id.desc(),
                        )
                        .limit(1)
                    )
                ).first()
                if success_row is not None:
                    output_blob_url = success_row[2]
                    if output_blob_url and output_blob_url.strip():
                        return NormalizationRun(
                            run_id=success_row[0],
                            attempt_no=success_row[1],
                            output_blob_url=output_blob_url,
                        )

                # Recovery path for legacy partial-completion defect.
                if pipeline_status != "NORMALIZED":
                    raise NormalizationStateError()

            await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _NORMALIZE_STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_FAILED,
                    ended_at=now,
                    error_code="InterruptedAttempt",
                    error_detail="Interrupted attempt.",
                )
            )

            max_attempt = (
                await self._conn.execute(
                    select(func.max(dmer_stage_run.c.attempt_no)).where(
                        dmer_stage_run.c.document_id == self._document_id,
                        dmer_stage_run.c.stage == _NORMALIZE_STAGE,
                    )
                )
            ).scalar_one()
            attempt_no = (max_attempt or 0) + 1

            doc_update = await self._conn.execute(
                dmer_document.update()
                .where(
                    dmer_document.c.id == self._document_id,
                    dmer_document.c.pipeline_status.in_(_READY_TO_START),
                )
                .values(
                    current_stage=_NORMALIZE_STAGE,
                    attempt_count=dmer_document.c.attempt_count + 1,
                    updated_at=now,
                )
                .returning(dmer_document.c.id)
            )
            if doc_update.first() is None:
                raise NormalizationStateError()

            run_id = (
                await self._conn.execute(
                    dmer_stage_run.insert()
                    .values(
                        document_id=self._document_id,
                        stage=_NORMALIZE_STAGE,
                        status=_RUNNING,
                        attempt_no=attempt_no,
                        started_at=now,
                        model_version=model_version,
                    )
                    .returning(dmer_stage_run.c.id)
                )
            ).scalar_one()

            return NormalizationRun(
                run_id=run_id,
                attempt_no=attempt_no,
                output_blob_url=None,
            )

    async def succeed(
        self,
        run_id: int,
        *,
        ended_at: datetime,
        output_blob_url: str,
        model_version: str,
    ) -> None:
        """Atomically mark both Normalize run and document as successful."""
        self._check_session()
        if not output_blob_url.strip():
            raise NormalizationStateError()

        async with self._conn.begin():
            stage_result = await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.id == run_id,
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _NORMALIZE_STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_SUCCEEDED,
                    ended_at=ended_at,
                    output_blob_url=output_blob_url,
                    model_version=model_version,
                    error_code=None,
                    error_detail=None,
                )
                .returning(dmer_stage_run.c.id)
            )
            if stage_result.first() is None:
                raise NormalizationStateError()

            doc_result = await self._conn.execute(
                dmer_document.update()
                .where(
                    dmer_document.c.id == self._document_id,
                    dmer_document.c.pipeline_status.in_(_READY_TO_START),
                )
                .values(
                    pipeline_status="NORMALIZED",
                    current_stage=_RULES_STAGE,
                    updated_at=ended_at,
                )
                .returning(dmer_document.c.id)
            )
            if doc_result.first() is None:
                raise NormalizationStateError()

    async def fail(
        self,
        run_id: int,
        *,
        ended_at: datetime,
        error_code: str,
        error_detail: str,
    ) -> None:
        """Mark a running Normalize run failed without regressing document status."""
        self._check_session()
        async with self._conn.begin():
            stage_result = await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.id == run_id,
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _NORMALIZE_STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_FAILED,
                    ended_at=ended_at,
                    error_code=error_code,
                    error_detail=error_detail,
                )
                .returning(dmer_stage_run.c.id)
            )
            if stage_result.first() is None:
                return  # already committed/finished; never downgrade success

            await self._conn.execute(
                dmer_document.update()
                .where(
                    dmer_document.c.id == self._document_id,
                    dmer_document.c.pipeline_status.in_(_READY_TO_START),
                )
                .values(updated_at=ended_at)
            )


@asynccontextmanager
async def normalization_session(engine: AsyncEngine, document_id: str):
    """Yield a per-document normalization repository bound to one connection.

    The session acquires a PostgreSQL advisory lock and keeps it for the full
    context lifetime; lock contention fails fast with ``NormalizationBusyError``.
    """
    async with advisory_lock_session(
        engine, f"normalize:{document_id}", busy_error=NormalizationBusyError
    ) as conn:
        yield NormalizationRepository(conn, document_id=document_id)
