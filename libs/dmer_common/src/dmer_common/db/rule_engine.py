"""DB unit of work for the Rule Engine activity.

See docs/development/stages/05-activity-rule-engine.md. Like Normalize's
(:mod:`.normalization`), every write for one document goes through one
dedicated connection holding a per-document advisory lock, so a retried or
replayed activity can neither interleave with another attempt nor half-apply.

On success, one transaction:

- inserts the ``rule_evaluation`` row -- every candidate outcome, not just
  the winner, plus the engine's own outcome, in ``all_outcomes`` (see
  :func:`all_outcomes_record`);
- marks the ``RULES`` stage run ``SUCCEEDED`` (``model_version`` = the
  rules version, the stage's version record, as Normalize records its model);
- moves the document to ``RULES_APPLIED`` / ``DECISION``;
- counts the document toward its driver's open ``driver_evaluation`` -- once
  only, via a ``driver_evaluation_document`` marker row.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..rules import RuleEvaluation
from ._advisory import advisory_lock_session
from .dmer_document import dmer_document
from .dmer_stage_run import dmer_stage_run

metadata = MetaData()

# Created by V0001 (rules_version, rule_evaluation, driver_evaluation) and
# V0004 (driver_evaluation_document).
rules_version = Table(
    "rules_version",
    metadata,
    Column("version", Text, primary_key=True),
    Column("blob_url", Text),
    Column("checksum", Text),
    Column("activated_at", DateTime(timezone=True)),
    Column("activated_by", Text),
)

rule_evaluation = Table(
    "rule_evaluation",
    metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("document_id", PG_UUID(as_uuid=False), nullable=False),
    Column("rules_version", Text, nullable=False),
    Column("all_outcomes", JSONB),
    Column("selected_outcome_code", Text),
    Column("selected_reason", Text),
    Column("priority_rank", Integer),
    Column("evaluated_at", DateTime(timezone=True)),
)


def all_outcomes_record(evaluation: RuleEvaluation) -> dict:
    """``rule_evaluation.all_outcomes``: every candidate the engine returned and
    the engine's own outcome. ``selected_outcome_code`` differs from
    ``rule_engine_outcome_code`` only when a clean pass needs the driving-record
    check (selected ``IN``); ``selected_reason`` then says why."""
    return {
        "rule_engine_outcome_code": evaluation.rule_engine_outcome_code,
        "candidates": evaluation.all_outcomes(),
    }


driver_evaluation = Table(
    "driver_evaluation",
    metadata,
    Column(
        "id",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("driver_key", PG_UUID(as_uuid=False), nullable=False),
    Column("open", Boolean, nullable=False),
    Column("completed_document_count", Integer, nullable=False),
)

driver_evaluation_document = Table(
    "driver_evaluation_document",
    metadata,
    Column("driver_evaluation_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("document_id", PG_UUID(as_uuid=False), primary_key=True),
    Column("counted_at", DateTime(timezone=True), nullable=False),
)


class RuleEngineBusyError(RuntimeError):
    """Another Rule Engine session already holds the document lock."""

    def __init__(self) -> None:
        super().__init__("Rule evaluation is already running for this document.")


class RuleEngineNotReadyError(RuntimeError):
    """The document hasn't been normalized yet."""

    def __init__(self) -> None:
        super().__init__("Rule evaluation is not ready for this document.")


class RuleEngineStateError(RuntimeError):
    """Missing document, manual review, or inconsistent Rule Engine state."""

    def __init__(self) -> None:
        super().__init__("Rule evaluation state is invalid for this operation.")


@dataclass(frozen=True)
class CompletedEvaluation:
    """A committed evaluation, returned instead of re-running on replay."""

    rule_evaluation_id: int
    rules_version: str
    selected_outcome_code: str


@dataclass(frozen=True)
class RuleEngineRun:
    run_id: int
    attempt_no: int
    received_date: datetime | None
    driver_key: str | None
    completed: CompletedEvaluation | None = None


@dataclass(frozen=True)
class DriverCount:
    """What happened to the driver's ``completed_document_count``."""

    counted: bool  # this call incremented it
    driver_evaluation_id: str | None  # the open evaluation, if there is one


_RULES_STAGE = "RULES"
_DECISION_STAGE = "DECISION"
_RUNNING = "RUNNING"
_SUCCEEDED = "SUCCEEDED"
_FAILED = "FAILED"

_READY_TO_START = {"NORMALIZED"}
_NOT_READY = {"RECEIVED", "DOWNLOADED", "EXTRACTING", "EXTRACTED"}
_REPLAY_ALLOWED = {
    "RULES_APPLIED",
    "AWAITING_DRIVER_COMPLETION",
    "DECIDED",
    "POSTING",
    "COMPLETED",
}

# A rules.json the publishing process hasn't registered gets this version name.
_UNREGISTERED_PREFIX = "sha256-"
UNREGISTERED_ACTIVATED_BY = "rule-engine-activity (unregistered active ruleset)"


class RuleEngineRepository:
    """Rule Engine DB operations bound to one dedicated async connection."""

    def __init__(self, conn: AsyncConnection, *, document_id: str) -> None:
        self._conn = conn
        self._document_id = document_id

    def _check_session(self) -> None:
        # A transparently reconnected connection would not own our advisory lock.
        if self._conn.closed or self._conn.invalidated:
            raise RuleEngineBusyError()

    async def start_or_resume(self, *, now: datetime) -> RuleEngineRun:
        """Start a new RULES attempt, or return the committed evaluation."""
        self._check_session()
        async with self._conn.begin():
            doc = (
                await self._conn.execute(
                    select(
                        dmer_document.c.pipeline_status,
                        dmer_document.c.received_date,
                        dmer_document.c.driver_key,
                    )
                    .where(dmer_document.c.id == self._document_id)
                    .with_for_update()
                )
            ).first()
            if doc is None:
                raise RuleEngineStateError()
            status, received_date, driver_key = doc

            if status in _NOT_READY:
                raise RuleEngineNotReadyError()
            if status in _REPLAY_ALLOWED:
                completed = await self._latest_success()
                if completed is None:
                    raise RuleEngineStateError()
                run_id, attempt_no, evaluation = completed
                return RuleEngineRun(
                    run_id, attempt_no, received_date, driver_key, evaluation
                )
            if status not in _READY_TO_START:
                raise RuleEngineStateError()  # MANUAL_REVIEW or unknown

            # A RUNNING row here crashed or lost its lock; close it.
            await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _RULES_STAGE,
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
                        dmer_stage_run.c.stage == _RULES_STAGE,
                    )
                )
            ).scalar_one()
            attempt_no = (max_attempt or 0) + 1

            await self._conn.execute(
                dmer_document.update()
                .where(dmer_document.c.id == self._document_id)
                .values(
                    current_stage=_RULES_STAGE,
                    attempt_count=dmer_document.c.attempt_count + 1,
                    updated_at=now,
                )
            )
            run_id = (
                await self._conn.execute(
                    dmer_stage_run.insert()
                    .values(
                        document_id=self._document_id,
                        stage=_RULES_STAGE,
                        status=_RUNNING,
                        attempt_no=attempt_no,
                        started_at=now,
                    )
                    .returning(dmer_stage_run.c.id)
                )
            ).scalar_one()
            return RuleEngineRun(run_id, attempt_no, received_date, driver_key)

    async def _latest_success(
        self,
    ) -> tuple[int, int, CompletedEvaluation] | None:
        """The latest successful attempt and the evaluation it committed --
        written in the same transaction, so the newest evaluation is its."""
        run = (
            await self._conn.execute(
                select(dmer_stage_run.c.id, dmer_stage_run.c.attempt_no)
                .where(
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _RULES_STAGE,
                    dmer_stage_run.c.status == _SUCCEEDED,
                )
                .order_by(
                    dmer_stage_run.c.attempt_no.desc(), dmer_stage_run.c.id.desc()
                )
                .limit(1)
            )
        ).first()
        evaluation = (
            await self._conn.execute(
                select(
                    rule_evaluation.c.id,
                    rule_evaluation.c.rules_version,
                    rule_evaluation.c.selected_outcome_code,
                )
                .where(rule_evaluation.c.document_id == self._document_id)
                .order_by(rule_evaluation.c.id.desc())
                .limit(1)
            )
        ).first()
        if run is None or evaluation is None:
            return None
        return run[0], run[1], CompletedEvaluation(*evaluation)

    async def register_rules_version(
        self, *, checksum: str, blob_url: str, now: datetime
    ) -> str:
        """The ``rules_version.version`` of the ruleset with *checksum*.

        The publishing process (not built yet) records each version it
        publishes. A ruleset nobody registered is recorded here under a
        checksum-derived name, so every evaluation still references the exact
        rules it used -- never silently, see ``UNREGISTERED_ACTIVATED_BY``.
        """
        self._check_session()
        async with self._conn.begin():
            existing = (
                await self._conn.execute(
                    select(rules_version.c.version)
                    .where(rules_version.c.checksum == checksum)
                    .order_by(rules_version.c.activated_at.desc().nulls_last())
                    .limit(1)
                )
            ).first()
            if existing is not None:
                return existing[0]
            version = f"{_UNREGISTERED_PREFIX}{checksum[:12]}"
            await self._conn.execute(
                pg_insert(rules_version)
                .values(
                    version=version,
                    blob_url=blob_url,
                    checksum=checksum,
                    activated_at=now,
                    activated_by=UNREGISTERED_ACTIVATED_BY,
                )
                .on_conflict_do_nothing(index_elements=["version"])
            )
            return version

    async def succeed(
        self,
        run_id: int,
        *,
        evaluation: RuleEvaluation,
        rules_version_name: str,
        driver_key: str | None,
        ended_at: datetime,
    ) -> tuple[int, DriverCount]:
        """Record the evaluation and complete the stage, atomically."""
        self._check_session()
        async with self._conn.begin():
            stage = await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.id == run_id,
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _RULES_STAGE,
                    dmer_stage_run.c.status == _RUNNING,
                )
                .values(
                    status=_SUCCEEDED,
                    ended_at=ended_at,
                    model_version=rules_version_name,
                    error_code=None,
                    error_detail=None,
                )
                .returning(dmer_stage_run.c.id)
            )
            if stage.first() is None:
                raise RuleEngineStateError()

            evaluation_id = (
                await self._conn.execute(
                    rule_evaluation.insert()
                    .values(
                        document_id=self._document_id,
                        rules_version=rules_version_name,
                        all_outcomes=all_outcomes_record(evaluation),
                        selected_outcome_code=evaluation.selected_outcome_code,
                        selected_reason=evaluation.selected_reason,
                        priority_rank=evaluation.priority_rank,
                        evaluated_at=ended_at,
                    )
                    .returning(rule_evaluation.c.id)
                )
            ).scalar_one()

            doc = await self._conn.execute(
                dmer_document.update()
                .where(
                    dmer_document.c.id == self._document_id,
                    dmer_document.c.pipeline_status.in_(_READY_TO_START),
                )
                .values(
                    pipeline_status="RULES_APPLIED",
                    current_stage=_DECISION_STAGE,
                    updated_at=ended_at,
                )
                .returning(dmer_document.c.id)
            )
            if doc.first() is None:
                raise RuleEngineStateError()

            count = await self._count_toward_driver(driver_key, ended_at)
            return evaluation_id, count

    async def _count_toward_driver(
        self, driver_key: str | None, now: datetime
    ) -> DriverCount:
        """Increment the open evaluation's count, once per document."""
        if not driver_key:
            return DriverCount(counted=False, driver_evaluation_id=None)
        open_evaluation = (
            await self._conn.execute(
                select(driver_evaluation.c.id).where(
                    driver_evaluation.c.driver_key == driver_key,
                    driver_evaluation.c.open.is_(True),
                )
            )
        ).first()
        if open_evaluation is None:
            return DriverCount(counted=False, driver_evaluation_id=None)
        evaluation_id = open_evaluation[0]
        marked = (
            await self._conn.execute(
                pg_insert(driver_evaluation_document)
                .values(
                    driver_evaluation_id=evaluation_id,
                    document_id=self._document_id,
                    counted_at=now,
                )
                .on_conflict_do_nothing()
                .returning(driver_evaluation_document.c.document_id)
            )
        ).first()
        if marked is None:
            return DriverCount(counted=False, driver_evaluation_id=evaluation_id)
        await self._conn.execute(
            driver_evaluation.update()
            .where(driver_evaluation.c.id == evaluation_id)
            .values(
                completed_document_count=driver_evaluation.c.completed_document_count
                + 1
            )
        )
        return DriverCount(counted=True, driver_evaluation_id=evaluation_id)

    async def fail(
        self, run_id: int, *, ended_at: datetime, error_code: str, error_detail: str
    ) -> None:
        """Mark a running attempt failed; never downgrades a committed success."""
        self._check_session()
        async with self._conn.begin():
            stage = await self._conn.execute(
                dmer_stage_run.update()
                .where(
                    dmer_stage_run.c.id == run_id,
                    dmer_stage_run.c.document_id == self._document_id,
                    dmer_stage_run.c.stage == _RULES_STAGE,
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
            if stage.first() is None:
                return
            await self._conn.execute(
                dmer_document.update()
                .where(
                    dmer_document.c.id == self._document_id,
                    dmer_document.c.pipeline_status.in_(_READY_TO_START),
                )
                .values(updated_at=ended_at)
            )


@asynccontextmanager
async def rule_engine_session(engine: AsyncEngine, document_id: str):
    """Yield a per-document Rule Engine repository bound to one connection
    holding the document's advisory lock (contention: ``RuleEngineBusyError``)."""
    async with advisory_lock_session(
        engine, f"rules:{document_id}", busy_error=RuleEngineBusyError
    ) as conn:
        yield RuleEngineRepository(conn, document_id=document_id)
