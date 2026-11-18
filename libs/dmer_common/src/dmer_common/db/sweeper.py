"""Reconciliation Sweeper: find work that stopped moving and move it again.

See docs/development/stages/09-reliability-components.md (architecture v2.1
4.6.2). Three of the four documented queries (the fourth -- documents in
Mercury's GET-by-licence response with no ``dmer_document`` row -- is not
built yet; see the stage doc):

1. :func:`sweep_stalled_documents` -- a document in an in-flight status
   (``RECEIVED`` .. ``NORMALIZED``) whose ``updated_at`` is older than its
   stage's SLA: re-publish it to the queue that status waits on, at most
   ``max_republish`` times per stage; then ``MANUAL_REVIEW`` for a person
   (no decision -- a stall is never a business failure).
2. :func:`sweep_stale_evaluations` -- an open ``WAITING`` driver evaluation
   whose documents have all reached ``RULES_APPLIED`` but which hasn't moved
   within the SLA (the lost-update race, 06-driver-orchestration.md):
   ``WAITING -> STALE`` and re-signal the driver on ``driver-decision``
   (the orchestrator takes ``STALE -> READY``).
3. :func:`outbox_backlog` -- ``mercury_outbox`` rows ``PENDING`` past a
   threshold or retried often: counts for the alert; nothing is written.
   ``FAILED`` rows are counted but don't alert here: each one alerts once,
   when the publisher gives up on it, and a person resolves it in Mercury --
   the row stays ``FAILED`` (no record of that in this system).

Each sweep commits its records first and returns the messages to publish:
if publishing fails, ``updated_at`` has moved on and the next sweep after the
SLA tries again. Every re-publish and give-up is a ``processing_error`` row
(``TRANSIENT_STALLED`` / ``UNKNOWN_STALLED``), so repeated stalls show as a
pattern; the count of those rows is the per-stage budget.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import and_, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..telemetry import get_logger
from .dmer_document import dmer_document
from .dmer_stage_run import dmer_stage_run
from .driver_decision import driver_evaluation
from .mercury_outbox import mercury_outbox
from .processing_error import _LEGACY_CLASS, FailureCategory, processing_error
from .status import PipelineStage, PipelineStatus

_log = get_logger(__name__)

TRANSIENT_STALLED: Final = "TRANSIENT_STALLED"
UNKNOWN_STALLED: Final = "UNKNOWN_STALLED"
REPUBLISHED: Final = "REPUBLISHED"
_S = PipelineStatus


class SweepQueue(str, enum.Enum):
    """Which pipeline queue a re-publish goes to."""

    INGEST = "ingest"
    RAW = "raw"
    EXTRACTED = "extracted"
    DRIVER_DECISION = "driver_decision"


# In-flight status -> (the stage it belongs to, the queue it waits on).
# RULES_APPLIED / AWAITING_DRIVER_COMPLETION wait on their driver's
# evaluation -- sweep_stale_evaluations. DECIDED / POSTING wait on the
# outbox -- outbox_backlog.
_IN_FLIGHT: Final = {
    _S.RECEIVED: (PipelineStage.INGEST, SweepQueue.INGEST),
    _S.DOWNLOADED: (PipelineStage.EXTRACT, SweepQueue.RAW),
    _S.EXTRACTING: (PipelineStage.EXTRACT, SweepQueue.RAW),
    _S.EXTRACTED: (PipelineStage.NORMALIZE, SweepQueue.EXTRACTED),
    _S.NORMALIZED: (PipelineStage.NORMALIZE, SweepQueue.EXTRACTED),
}
_PRE_RULES: Final = tuple(s.value for s in _IN_FLIGHT)
_AWAITING_DRIVER: Final = (
    _S.RULES_APPLIED.value,
    _S.AWAITING_DRIVER_COMPLETION.value,
)


@dataclass(frozen=True)
class SweeperSettings:
    # How long a document may sit in a stage's statuses (question I-14).
    stage_sla: dict[PipelineStage, timedelta] = field(
        default_factory=lambda: {
            PipelineStage.INGEST: timedelta(minutes=30),
            PipelineStage.EXTRACT: timedelta(minutes=30),
            PipelineStage.NORMALIZE: timedelta(minutes=30),
        }
    )
    driver_sla: timedelta = timedelta(minutes=30)
    max_republish: int = 3  # per document and stage
    batch_size: int = 50
    outbox_pending_sla: timedelta = timedelta(hours=1)
    outbox_attempt_alert: int = 5


@dataclass(frozen=True)
class Republish:
    """One message to put back on a pipeline queue."""

    queue: SweepQueue
    document_id: str
    document_guid: str
    driver_key: str | None
    blob_url: str | None
    sweep: int  # this re-publish's number for the stage (1, 2, ...)


@dataclass
class DocumentSweep:
    republish: list[Republish] = field(default_factory=list)
    manual_review: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OutboxBacklog:
    pending_past_sla: int
    high_attempts: int
    failed: int

    @property
    def alert(self) -> bool:
        # FAILED rows alert once, from the publisher; a person resolves them
        # in Mercury and they stay FAILED, so counting them here would alert
        # every sweep forever.
        return bool(self.pending_past_sla or self.high_attempts)


async def _latest_output(
    conn: AsyncConnection, document_id: str, stage: PipelineStage
) -> str | None:
    return (
        await conn.execute(
            select(dmer_stage_run.c.output_blob_url)
            .where(
                dmer_stage_run.c.document_id == document_id,
                dmer_stage_run.c.stage == stage.value,
                dmer_stage_run.c.status == "SUCCEEDED",
                dmer_stage_run.c.output_blob_url.is_not(None),
            )
            .order_by(dmer_stage_run.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _prior_sweeps(
    conn: AsyncConnection, document_id: str, stage: PipelineStage
) -> int:
    return (
        await conn.execute(
            select(func.count())
            .select_from(processing_error)
            .where(
                processing_error.c.document_id == document_id,
                processing_error.c.stage == stage.value,
                processing_error.c.reason_code == TRANSIENT_STALLED,
            )
        )
    ).scalar_one()


async def _record(
    conn: AsyncConnection,
    *,
    document_id: str,
    stage: PipelineStage,
    category: FailureCategory,
    reason_code: str,
    message: str,
    sweep: int,
    resolution: str,
    now: datetime,
) -> None:
    await conn.execute(
        processing_error.insert().values(
            document_id=document_id,
            stage=stage.value,
            error_class=_LEGACY_CLASS[category],
            failure_category=category.value,
            reason_code=reason_code,
            message=message,
            redrive_count=sweep,
            occurred_at=now,
            resolution=resolution,
            resolved_at=now if resolution == REPUBLISHED else None,
        )
    )


async def _republish_target(
    conn: AsyncConnection, row, queue: SweepQueue
) -> tuple[SweepQueue, str | None]:
    """The queue and blob the stalled document's message needs. A missing
    artifact falls back to the stage before it."""
    if queue is SweepQueue.EXTRACTED:
        blob = await _latest_output(conn, row.id, PipelineStage.EXTRACT)
        if blob:
            return queue, blob
        queue = SweepQueue.RAW
    if queue is SweepQueue.RAW:
        if row.raw_blob_url:
            return queue, row.raw_blob_url
        queue = SweepQueue.INGEST
    return SweepQueue.INGEST, None


async def sweep_stalled_documents(
    engine: AsyncEngine, *, settings: SweeperSettings, now: datetime
) -> DocumentSweep:
    """Query 1: re-publish (or give up on) documents stalled in a stage."""
    result = DocumentSweep()
    stalled = [
        and_(
            dmer_document.c.pipeline_status == status.value,
            dmer_document.c.updated_at < now - settings.stage_sla[stage],
        )
        for status, (stage, _) in _IN_FLIGHT.items()
    ]
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    dmer_document.c.id,
                    dmer_document.c.document_guid,
                    dmer_document.c.driver_key,
                    dmer_document.c.pipeline_status,
                    dmer_document.c.raw_blob_url,
                )
                .where(or_(*stalled))
                .order_by(dmer_document.c.updated_at)
                .limit(settings.batch_size)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for row in rows:
            status = PipelineStatus(
                str(getattr(row.pipeline_status, "value", row.pipeline_status))
            )
            stage, queue = _IN_FLIGHT[status]
            prior = await _prior_sweeps(conn, row.id, stage)
            if prior >= settings.max_republish:
                await conn.execute(
                    dmer_document.update()
                    .where(dmer_document.c.id == row.id)
                    .values(pipeline_status=_S.MANUAL_REVIEW.value, updated_at=now)
                )
                await _record(
                    conn,
                    document_id=row.id,
                    stage=stage,
                    category=FailureCategory.UNKNOWN,
                    reason_code=UNKNOWN_STALLED,
                    message=f"stalled in {status.value} after {prior} re-publishes",
                    sweep=prior,
                    resolution=_S.MANUAL_REVIEW.value,
                    now=now,
                )
                result.manual_review.append(row.id)
                _log.error(
                    "stalled document routed to manual review",
                    extra={
                        "document_id": row.id,
                        "pipeline_status": status.value,
                        "stage": stage.value,
                        "republishes": prior,
                    },
                )
                continue
            target, blob = await _republish_target(conn, row, queue)
            await conn.execute(
                dmer_document.update()
                .where(dmer_document.c.id == row.id)
                .values(attempt_count=dmer_document.c.attempt_count + 1, updated_at=now)
            )
            await _record(
                conn,
                document_id=row.id,
                stage=stage,
                category=FailureCategory.TRANSIENT,
                reason_code=TRANSIENT_STALLED,
                message=f"stalled in {status.value}; re-published to {target.value}",
                sweep=prior + 1,
                resolution=REPUBLISHED,
                now=now,
            )
            result.republish.append(
                Republish(
                    queue=target,
                    document_id=row.id,
                    document_guid=str(row.document_guid),
                    driver_key=str(row.driver_key) if row.driver_key else None,
                    blob_url=blob,
                    sweep=prior + 1,
                )
            )
            _log.warning(
                "stalled document re-published",
                extra={
                    "document_id": row.id,
                    "pipeline_status": status.value,
                    "queue": target.value,
                    "sweep": prior + 1,
                },
            )
    return result


async def sweep_stale_evaluations(
    engine: AsyncEngine, *, settings: SweeperSettings, now: datetime
) -> list[Republish]:
    """Query 2: re-signal drivers whose open evaluation is stuck in WAITING
    although none of their documents is still in flight.

    The last activity is the newer of the evaluation's last Mercury check and
    its documents' last update; past ``driver_sla`` the evaluation goes
    ``WAITING -> STALE`` and one ``driver-decision`` signal is returned for
    the driver. A ``STALE`` evaluation is re-signalled again after the next
    SLA, at most ``max_republish`` times (counted on the signalling
    document's ``DECISION`` stage); after that it is left ``STALE`` and
    logged at ERROR for a person.
    """
    driver_docs = dmer_document.alias("driver_docs")
    in_flight = exists().where(
        driver_docs.c.driver_key == driver_evaluation.c.driver_key,
        driver_docs.c.pipeline_status.in_(_PRE_RULES),
    )
    awaiting = exists().where(
        driver_docs.c.driver_key == driver_evaluation.c.driver_key,
        driver_docs.c.pipeline_status.in_(_AWAITING_DRIVER),
    )
    last_document_update = (
        select(func.max(driver_docs.c.updated_at))
        .where(driver_docs.c.driver_key == driver_evaluation.c.driver_key)
        .scalar_subquery()
    )
    last_activity = func.greatest(
        func.coalesce(driver_evaluation.c.last_mercury_check_at, last_document_update),
        last_document_update,
    )
    signals: list[Republish] = []
    async with engine.begin() as conn:
        evaluations = (
            await conn.execute(
                select(
                    driver_evaluation.c.id,
                    driver_evaluation.c.driver_key,
                    driver_evaluation.c.status,
                )
                .where(
                    driver_evaluation.c.open.is_(True),
                    driver_evaluation.c.status.in_(("WAITING", "STALE")),
                    ~in_flight,
                    awaiting,
                    last_activity < now - settings.driver_sla,
                )
                .limit(settings.batch_size)
                .with_for_update(of=driver_evaluation, skip_locked=True)
            )
        ).all()
        for evaluation in evaluations:
            driver_key = str(evaluation.driver_key)
            document = (
                await conn.execute(
                    select(dmer_document.c.id, dmer_document.c.document_guid)
                    .where(
                        dmer_document.c.driver_key == driver_key,
                        dmer_document.c.pipeline_status.in_(_AWAITING_DRIVER),
                    )
                    .order_by(dmer_document.c.updated_at.desc())
                    .limit(1)
                )
            ).one()
            prior = await _prior_sweeps(conn, document.id, PipelineStage.DECISION)
            if prior >= settings.max_republish:
                _log.error(
                    "stale driver evaluation not re-signalled: budget used up",
                    extra={
                        "driver_evaluation_id": str(evaluation.id),
                        "signals": prior,
                    },
                )
                continue
            await conn.execute(
                driver_evaluation.update()
                .where(
                    driver_evaluation.c.id == evaluation.id,
                    driver_evaluation.c.status == "WAITING",
                )
                .values(status="STALE")
            )
            # Restart the SLA clock on the batch.
            await conn.execute(
                dmer_document.update()
                .where(dmer_document.c.id == document.id)
                .values(updated_at=now)
            )
            await _record(
                conn,
                document_id=document.id,
                stage=PipelineStage.DECISION,
                category=FailureCategory.TRANSIENT,
                reason_code=TRANSIENT_STALLED,
                message="driver evaluation stalled in WAITING; driver re-signalled",
                sweep=prior + 1,
                resolution=REPUBLISHED,
                now=now,
            )
            signals.append(
                Republish(
                    queue=SweepQueue.DRIVER_DECISION,
                    document_id=document.id,
                    document_guid=str(document.document_guid),
                    driver_key=driver_key,
                    blob_url=await _latest_output(
                        conn, document.id, PipelineStage.NORMALIZE
                    ),
                    sweep=prior + 1,
                )
            )
            _log.warning(
                "stale driver evaluation re-signalled",
                extra={"driver_evaluation_id": str(evaluation.id), "sweep": prior + 1},
            )
    return signals


async def outbox_backlog(
    engine: AsyncEngine, *, settings: SweeperSettings, now: datetime
) -> OutboxBacklog:
    """Query 3: the outbox rows an operator needs to hear about."""
    pending = mercury_outbox.c.status == "PENDING"
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(
                    func.count().filter(
                        pending,
                        mercury_outbox.c.next_attempt_at
                        < now - settings.outbox_pending_sla,
                    ),
                    func.count().filter(
                        pending,
                        mercury_outbox.c.attempt_count >= settings.outbox_attempt_alert,
                    ),
                    func.count().filter(mercury_outbox.c.status == "FAILED"),
                )
            )
        ).one()
    backlog = OutboxBacklog(*row)
    if backlog.alert:
        _log.error(
            "mercury outbox backlog",
            extra={
                "pending_past_sla": backlog.pending_past_sla,
                "high_attempts": backlog.high_attempts,
                "failed": backlog.failed,
            },
        )
    return backlog
