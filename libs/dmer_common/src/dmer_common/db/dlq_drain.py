"""DLQ Drain: turn one dead-lettered message into a recorded, visible result.

See docs/development/stages/10-dlq-drain.md. :func:`drain_dead_letter` runs
in **one transaction** per message:

1. **Already drained?** A ``processing_error`` row for this
   ``(document_id, stage, dlq_message_id)`` means a previous run recorded it
   but died before completing it on Service Bus: return what that run
   decided, write nothing.
2. **Classify** (:func:`dmer_common.dlq.classify`), using the document's last
   ``dmer_stage_run.error_code`` for the stage.
3. **Route:**

   - retriable, under ``max_redrives`` -> **redrive**: reopen the document at
     its queue's entry status (``status.REDRIVE_REOPENABLE``) and record the
     row with ``resolution = REDRIVEN``; the caller re-sends the message;
   - retriable, budget used up -> ``UNKNOWN`` / ``UNKNOWN_REDRIVE_EXHAUSTED``,
     then as below;
   - ``PERMANENT_BUSINESS`` -> ``MANUAL_REVIEW`` and the fallback IN outcome
     ("AI could not process", question I-1) queued to ``mercury_outbox``,
     unless the document already has a decision or a queued Mercury write;
   - ``UNKNOWN`` -> ``MANUAL_REVIEW``, no decision.

   A dead letter for a document that has since moved on (another delivery
   succeeded) is recorded as ``STALE`` and changes nothing.

Never a ``dmer_decision`` row: a fallback here has no driver evaluation to
hang it on (same as Driver Lookup's I-12/I-22 fallback), so the outcome is
the ``UPDATE_OUTCOME`` outbox row alone, keyed ``<document_id>:FALLBACK`` --
the same key Driver Lookup uses, so a document gets at most one fallback.

``processing_error.message`` holds the dead-letter reason and description,
which are codes and safe facts only (the DI processor's ``FailureCode`` and
``key=value`` detail; Service Bus system text) -- never document content.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import exists, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..dlq import (
    FALLBACK_REASONS,
    UNKNOWN_REDRIVE_EXHAUSTED,
    Classification,
    classify,
)
from ..telemetry import get_logger
from .dmer_document import dmer_document
from .dmer_stage_run import dmer_stage_run
from .driver_decision import dmer_decision
from .mercury_outbox import (
    fallback_comment,
    fallback_rows,
    mercury_outbox,
    queue_outbox_rows,
)
from .processing_error import _LEGACY_CLASS, FailureCategory, processing_error
from .status import REDRIVE_REOPENABLE, PipelineStatus

_log = get_logger(__name__)

_MESSAGE_MAX: Final = 500
_MANUAL_REVIEW: Final = PipelineStatus.MANUAL_REVIEW.value
# Statuses past which a dead letter can't change anything: decided/posted.
_SETTLED: Final = frozenset(
    {
        PipelineStatus.DECIDED.value,
        PipelineStatus.POSTING.value,
        PipelineStatus.COMPLETED.value,
    }
)


class DrainAction(str, enum.Enum):
    """What the drain did with a dead letter (``processing_error.resolution``)."""

    REDRIVEN = "REDRIVEN"  # caller re-sends it to the queue
    FALLBACK_QUEUED = "FALLBACK_QUEUED"  # MANUAL_REVIEW + fallback IN queued
    MANUAL_REVIEW = "MANUAL_REVIEW"  # MANUAL_REVIEW, no decision
    RECORDED = "RECORDED"  # recorded only (a driver-level message)
    STALE = "STALE"  # the document has moved on; nothing changed
    NO_DOCUMENT = "NO_DOCUMENT"  # no dmer_document row to attach it to


class _DrainedConcurrently(Exception):
    """Another drain recorded the same message first (rolls this one back)."""


@dataclass(frozen=True)
class DeadLetter:
    """One dead-lettered message, as the drain needs it."""

    queue: str
    stage: str  # dmer_stage of the queue's consumer
    message_id: str
    document_id: str | None
    reason: str | None
    description: str | None
    # The status this queue's stage starts from (a REDRIVE_REOPENABLE key);
    # None for a driver-level queue whose redrive doesn't touch documents.
    entry_status: PipelineStatus | None
    # Whether a non-retriable failure routes the document to MANUAL_REVIEW
    # (False for driver-level messages: the batch, not one document, failed).
    routes_document: bool = True


@dataclass(frozen=True)
class DrainResult:
    action: DrainAction
    classification: Classification | None
    redrive_attempt: int = 0  # this redrive's number (1, 2, ...) when REDRIVEN
    already_drained: bool = False


def _message(dead_letter: DeadLetter) -> str:
    text = ": ".join(
        part for part in (dead_letter.reason, dead_letter.description) if part
    )
    return (text or "no dead-letter reason")[:_MESSAGE_MAX]


async def _has_decision(conn: AsyncConnection, document_id: str) -> bool:
    """A decision or a queued Mercury write exists -- never reopen or add a
    second outcome (I-2: no revising a posted outcome)."""
    return bool(
        (
            await conn.execute(
                select(
                    exists().where(dmer_decision.c.document_id == document_id)
                    | exists().where(mercury_outbox.c.document_id == document_id)
                )
            )
        ).scalar()
    )


async def _last_error_code(
    conn: AsyncConnection, document_id: str, stage: str
) -> str | None:
    return (
        await conn.execute(
            select(dmer_stage_run.c.error_code)
            .where(
                dmer_stage_run.c.document_id == document_id,
                dmer_stage_run.c.stage == stage,
                dmer_stage_run.c.error_code.is_not(None),
            )
            .order_by(dmer_stage_run.c.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _prior_redrives(conn: AsyncConnection, document_id: str, stage: str) -> int:
    return (
        await conn.execute(
            select(func.count())
            .select_from(processing_error)
            .where(
                processing_error.c.document_id == document_id,
                processing_error.c.stage == stage,
                processing_error.c.resolution == DrainAction.REDRIVEN.value,
            )
        )
    ).scalar_one()


async def _record(
    conn: AsyncConnection,
    dead_letter: DeadLetter,
    classification: Classification,
    action: DrainAction,
    *,
    redrive_count: int,
    now: datetime,
) -> bool:
    """Insert the processing_error row; False if this message was already
    recorded (a concurrent drain got there first)."""
    inserted = await conn.execute(
        pg_insert(processing_error)
        .values(
            document_id=dead_letter.document_id,
            stage=dead_letter.stage,
            error_class=_LEGACY_CLASS[classification.category],
            failure_category=classification.category.value,
            reason_code=classification.reason_code,
            message=_message(dead_letter),
            dlq_message_id=dead_letter.message_id,
            redrive_count=redrive_count,
            occurred_at=now,
            resolution=action.value,
            resolved_at=now if action is DrainAction.REDRIVEN else None,
        )
        .on_conflict_do_nothing(
            index_elements=[
                processing_error.c.document_id,
                processing_error.c.stage,
                processing_error.c.dlq_message_id,
            ],
            index_where=processing_error.c.dlq_message_id.is_not(None),
        )
        .returning(processing_error.c.id)
    )
    return inserted.first() is not None


async def _already_drained(
    conn: AsyncConnection, dead_letter: DeadLetter
) -> DrainResult | None:
    row = (
        await conn.execute(
            select(
                processing_error.c.resolution,
                processing_error.c.failure_category,
                processing_error.c.reason_code,
                processing_error.c.redrive_count,
            ).where(
                processing_error.c.document_id == dead_letter.document_id,
                processing_error.c.stage == dead_letter.stage,
                processing_error.c.dlq_message_id == dead_letter.message_id,
            )
        )
    ).first()
    if row is None:
        return None
    return DrainResult(
        action=DrainAction(row.resolution),
        classification=Classification(
            FailureCategory(row.failure_category), row.reason_code
        ),
        redrive_attempt=row.redrive_count or 0,
        already_drained=True,
    )


async def drain_dead_letter(
    engine: AsyncEngine,
    dead_letter: DeadLetter,
    *,
    max_redrives: int,
    now: datetime,
) -> DrainResult:
    """Record and route one dead letter (see the module docstring)."""
    log_extra = {
        "document_id": dead_letter.document_id,
        "stage": dead_letter.stage,
        "queue": dead_letter.queue,
        "dlq_message_id": dead_letter.message_id,
        "dead_letter_reason": dead_letter.reason,
    }
    if not dead_letter.document_id:
        _log.error("dead letter has no document id; not recorded", extra=log_extra)
        return DrainResult(DrainAction.NO_DOCUMENT, None)

    try:
        result = await _drain(engine, dead_letter, max_redrives=max_redrives, now=now)
    except _DrainedConcurrently:
        _log.info("dead letter drained concurrently", extra=log_extra)
        async with engine.connect() as conn:
            previous = await _already_drained(conn, dead_letter)
        return previous or DrainResult(DrainAction.STALE, None, already_drained=True)
    if result.already_drained or result.action is DrainAction.NO_DOCUMENT:
        return result
    log = _log.warning if result.action is DrainAction.REDRIVEN else _log.error
    log(
        "dead letter drained",
        extra={
            **log_extra,
            "failure_category": result.classification.category.value,
            "reason_code": result.classification.reason_code,
            "action": result.action.value,
            "redrive_attempt": result.redrive_attempt,
            # Unambiguous in the logs: only FALLBACK_QUEUED is a decision.
            "decided_by": (
                "FALLBACK" if result.action is DrainAction.FALLBACK_QUEUED else None
            ),
        },
    )
    return result


async def _drain(
    engine: AsyncEngine,
    dead_letter: DeadLetter,
    *,
    max_redrives: int,
    now: datetime,
) -> DrainResult:
    log_extra = {"document_id": dead_letter.document_id, "stage": dead_letter.stage}
    async with engine.begin() as conn:
        status = (
            await conn.execute(
                select(dmer_document.c.pipeline_status)
                .where(dmer_document.c.id == dead_letter.document_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if status is None:
            _log.error(
                "dead letter for an unknown document; not recorded", extra=log_extra
            )
            return DrainResult(DrainAction.NO_DOCUMENT, None)
        status = str(getattr(status, "value", status))

        previous = await _already_drained(conn, dead_letter)
        if previous is not None:
            _log.info("dead letter already drained", extra=log_extra)
            return previous

        classification = classify(
            dead_letter.reason,
            dead_letter.description,
            await _last_error_code(conn, dead_letter.document_id, dead_letter.stage),
        )
        decided = await _has_decision(conn, dead_letter.document_id)
        redrive_count = 0

        if classification.retriable:
            prior = await _prior_redrives(
                conn, dead_letter.document_id, dead_letter.stage
            )
            if prior >= max_redrives:
                classification = Classification(
                    FailureCategory.UNKNOWN, UNKNOWN_REDRIVE_EXHAUSTED
                )
            else:
                redrive_count = prior + 1

        if classification.retriable:
            action = DrainAction.REDRIVEN
            entry = dead_letter.entry_status
            if entry is not None:
                reopenable = {s.value for s in REDRIVE_REOPENABLE[entry]}
                if status not in reopenable or decided:
                    action, redrive_count = DrainAction.STALE, 0
                else:
                    await conn.execute(
                        dmer_document.update()
                        .where(dmer_document.c.id == dead_letter.document_id)
                        .values(
                            pipeline_status=entry.value,
                            attempt_count=dmer_document.c.attempt_count + 1,
                            updated_at=now,
                        )
                    )
        elif status in _SETTLED or (decided and status != _MANUAL_REVIEW):
            action = DrainAction.STALE
        elif not dead_letter.routes_document:
            action = DrainAction.RECORDED
        else:
            if status != _MANUAL_REVIEW:
                await conn.execute(
                    dmer_document.update()
                    .where(dmer_document.c.id == dead_letter.document_id)
                    .values(pipeline_status=_MANUAL_REVIEW, updated_at=now)
                )
            action = DrainAction.MANUAL_REVIEW
            if (
                classification.category is FailureCategory.PERMANENT_BUSINESS
                and not decided
            ):
                guid = (
                    await conn.execute(
                        select(dmer_document.c.document_guid).where(
                            dmer_document.c.id == dead_letter.document_id
                        )
                    )
                ).scalar_one()
                await queue_outbox_rows(
                    conn,
                    fallback_rows(
                        source_id=f"{dead_letter.document_id}:FALLBACK",
                        document_id=dead_letter.document_id,
                        document_guid=str(guid),
                        comment=fallback_comment(
                            FALLBACK_REASONS.get(classification.reason_code, "")
                        ),
                    ),
                    now=now,
                )
                action = DrainAction.FALLBACK_QUEUED

        if not await _record(
            conn,
            dead_letter,
            classification,
            action,
            redrive_count=redrive_count,
            now=now,
        ):
            raise _DrainedConcurrently()  # rolls back this run's writes
    return DrainResult(action, classification, redrive_attempt=redrive_count)
