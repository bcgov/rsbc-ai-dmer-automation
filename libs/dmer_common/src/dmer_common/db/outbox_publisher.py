"""The Outbox Publisher: drains ``mercury_outbox`` to Mercury.

See docs/development/stages/09-reliability-components.md (architecture v2.1
4.6.1). A timer function calls :func:`publish_due` about once a minute:

1. **Claim** up to ``batch_size`` documents with a PENDING row that is due,
   with *all* their PENDING rows, oldest first, skipping rows another run has
   locked. Claiming leases the rows (``next_attempt_at`` moved ``lease``
   ahead), so a run that dies mid-send leaves them to be retried, not lost.
2. **Send** each document's rows in :data:`OPERATION_ORDER` (driver, case,
   duplicate, outcome), with the row's idempotency key, outside any
   transaction. A failure stops that document: the rows after it wait for
   the failed one, so an outcome is never written before its driver or case.
3. **Record**: ``SENT`` (``sent_at``, ``mercury_response``); or the attempt
   count, ``last_error`` and an exponential-backoff ``next_attempt_at``; or
   ``FAILED`` when Mercury rejected the write (a 4xx retrying won't fix) or
   after ``max_attempts`` -- logged at ERROR for the alert. The document's
   later rows then fail too ("blocked"), rather than being sent out of order;
   a person resolves the document.
4. **Roll up**: a document whose rows are all ``SENT`` goes ``POSTING`` ->
   ``COMPLETED``; a ``DECIDED`` driver evaluation whose documents' rows are
   all ``SENT`` goes to ``POSTED``. Documents in ``MANUAL_REVIEW`` (fallback
   outcomes) keep their status.

Logs carry ids, operations, keys and HTTP statuses only -- payloads carry
clinical content and licences.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from ..mercury_client import MercuryClient, MercuryWriteError
from ..telemetry import get_logger
from .dmer_document import dmer_document
from .driver_decision import dmer_decision, driver_evaluation
from .mercury_outbox import OPERATION_ORDER, OutboxOperation, mercury_outbox

_log = get_logger(__name__)

_ORDER = {op.value: i for i, op in enumerate(OPERATION_ORDER)}


@dataclass(frozen=True)
class PublisherSettings:
    batch_size: int = 20  # documents per run (Mercury throttling: M-10)
    max_attempts: int = 10
    backoff_initial: timedelta = timedelta(minutes=1)
    backoff_max: timedelta = timedelta(hours=1)
    lease: timedelta = timedelta(minutes=5)


@dataclass(frozen=True)
class ClaimedRow:
    id: str
    document_id: str
    operation: str
    payload: dict[str, Any]  # clinical content: never log
    idempotency_key: str
    attempt_count: int


@dataclass
class PublishSummary:
    documents: int = 0
    sent: int = 0
    retrying: int = 0
    failed: int = 0
    documents_completed: int = 0
    evaluations_posted: int = 0
    errors: list[str] = field(default_factory=list)


def backoff(attempts: int, settings: PublisherSettings) -> timedelta:
    """Wait before the next attempt, after *attempts* failed ones: doubling
    from ``backoff_initial``, capped at ``backoff_max``."""
    wait = settings.backoff_initial * (2 ** max(attempts - 1, 0))
    return min(wait, settings.backoff_max)


async def claim_due(
    engine: AsyncEngine, *, now: datetime, settings: PublisherSettings
) -> dict[str, list[ClaimedRow]]:
    """Lease the PENDING rows of up to ``batch_size`` documents with a due
    row, oldest first. Returns ``{document_id: rows in OPERATION_ORDER}``."""
    o = mercury_outbox
    due = (
        select(o.c.document_id)
        .where(o.c.status == "PENDING", o.c.next_attempt_at <= now)
        .group_by(o.c.document_id)
        .order_by(func.min(o.c.next_attempt_at))
        .limit(settings.batch_size)
    )
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                select(
                    o.c.id,
                    o.c.document_id,
                    o.c.operation,
                    o.c.payload,
                    o.c.idempotency_key,
                    o.c.attempt_count,
                )
                .where(o.c.status == "PENDING", o.c.document_id.in_(due))
                .with_for_update(skip_locked=True)
            )
        ).all()
        if rows:
            await conn.execute(
                update(o)
                .where(o.c.id.in_([r.id for r in rows]))
                .values(next_attempt_at=now + settings.lease)
            )
    claimed: dict[str, list[ClaimedRow]] = {}
    for r in rows:
        claimed.setdefault(str(r.document_id), []).append(
            ClaimedRow(
                id=str(r.id),
                document_id=str(r.document_id),
                operation=str(r.operation),
                payload=r.payload,
                idempotency_key=r.idempotency_key,
                attempt_count=r.attempt_count,
            )
        )
    for doc_rows in claimed.values():
        doc_rows.sort(key=lambda r: _ORDER[r.operation])
    return claimed


async def _record_sent(
    engine: AsyncEngine, row: ClaimedRow, response: dict[str, Any], now: datetime
) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(mercury_outbox)
            .where(mercury_outbox.c.id == row.id, mercury_outbox.c.status == "PENDING")
            .values(
                status="SENT",
                sent_at=now,
                mercury_response=response,
                attempt_count=row.attempt_count + 1,
                last_error=None,
            )
        )


async def _record_failure(
    engine: AsyncEngine,
    row: ClaimedRow,
    later: list[ClaimedRow],
    error: MercuryWriteError,
    *,
    now: datetime,
    settings: PublisherSettings,
) -> str:
    """Record a failed attempt; returns the row's new status. The rows after
    it (*later*) wait until its next attempt, or fail with it as blocked."""
    attempts = row.attempt_count + 1
    give_up = not error.retryable or attempts >= settings.max_attempts
    next_at = now + backoff(attempts, settings)
    async with engine.begin() as conn:
        await conn.execute(
            update(mercury_outbox)
            .where(mercury_outbox.c.id == row.id, mercury_outbox.c.status == "PENDING")
            .values(
                status="FAILED" if give_up else "PENDING",
                attempt_count=attempts,
                last_error=str(error)[:500],
                next_attempt_at=None if give_up else next_at,
            )
        )
        if later:
            values: dict[str, Any] = (
                {
                    "status": "FAILED",
                    "next_attempt_at": None,
                    "last_error": f"blocked: {row.operation} failed",
                }
                if give_up
                else {"next_attempt_at": next_at}
            )
            await conn.execute(
                update(mercury_outbox)
                .where(
                    mercury_outbox.c.id.in_([r.id for r in later]),
                    mercury_outbox.c.status == "PENDING",
                )
                .values(**values)
            )
    return "FAILED" if give_up else "PENDING"


async def _roll_up(
    engine: AsyncEngine, document_id: str, now: datetime
) -> tuple[bool, bool]:
    """POSTING -> COMPLETED for the document, DECIDED -> POSTED for its
    evaluation, when every row involved is SENT. Returns what moved."""
    o, doc, dec, ev = mercury_outbox, dmer_document, dmer_decision, driver_evaluation
    async with engine.begin() as conn:
        unsent = (
            await conn.execute(
                select(func.count())
                .select_from(o)
                .where(o.c.document_id == document_id, o.c.status != "SENT")
            )
        ).scalar_one()
        if unsent:
            return False, False
        completed = (
            await conn.execute(
                update(doc)
                .where(doc.c.id == document_id, doc.c.pipeline_status == "POSTING")
                .values(pipeline_status="COMPLETED", updated_at=now)
                .returning(doc.c.id)
            )
        ).first() is not None
        evaluation_id = (
            await conn.execute(
                select(dec.c.driver_evaluation_id).where(
                    dec.c.document_id == document_id
                )
            )
        ).scalar_one_or_none()
        if evaluation_id is None:
            return completed, False
        batch = select(dec.c.document_id).where(
            dec.c.driver_evaluation_id == evaluation_id
        )
        batch_unsent = (
            await conn.execute(
                select(func.count())
                .select_from(o)
                .where(o.c.document_id.in_(batch), o.c.status != "SENT")
            )
        ).scalar_one()
        posted = False
        if not batch_unsent:
            posted = (
                await conn.execute(
                    update(ev)
                    .where(ev.c.id == evaluation_id, ev.c.status == "DECIDED")
                    .values(status="POSTED")
                    .returning(ev.c.id)
                )
            ).first() is not None
        return completed, posted


async def publish_due(
    engine: AsyncEngine,
    client: MercuryClient,
    *,
    settings: PublisherSettings | None = None,
    clock: Callable[[], datetime],
) -> PublishSummary:
    """One publisher run: claim, send in order, record, roll up."""
    settings = settings or PublisherSettings()
    summary = PublishSummary()
    claimed = await claim_due(engine, now=clock(), settings=settings)
    summary.documents = len(claimed)
    for document_id, rows in claimed.items():
        for i, row in enumerate(rows):
            log = {
                "document_id": document_id,
                "operation": row.operation,
                "idempotency_key": row.idempotency_key,
                "attempt": row.attempt_count + 1,
            }
            try:
                response = await asyncio.to_thread(
                    client.send_outbox, row.operation, row.payload, row.idempotency_key
                )
            except MercuryWriteError as exc:
                status = await _record_failure(
                    engine, row, rows[i + 1 :], exc, now=clock(), settings=settings
                )
                if status == "FAILED":
                    summary.failed += 1
                    _log.error(
                        "outbox row failed permanently",
                        extra={
                            **log,
                            "http_status": exc.status,
                            "retryable": exc.retryable,
                        },
                    )
                else:
                    summary.retrying += 1
                    _log.warning(
                        "outbox row will be retried",
                        extra={**log, "http_status": exc.status},
                    )
                break  # the rest of this document waits for this row
            await _record_sent(engine, row, response, clock())
            summary.sent += 1
            _log.info("outbox row sent", extra=log)
        completed, posted = await _roll_up(engine, document_id, clock())
        summary.documents_completed += completed
        summary.evaluations_posted += posted
    _log.info(
        "outbox publisher run",
        extra={
            "documents": summary.documents,
            "sent": summary.sent,
            "retrying": summary.retrying,
            "failed": summary.failed,
            "documents_completed": summary.documents_completed,
            "evaluations_posted": summary.evaluations_posted,
        },
    )
    return summary


__all__ = [
    "ClaimedRow",
    "OutboxOperation",
    "PublishSummary",
    "PublisherSettings",
    "backoff",
    "claim_due",
    "publish_due",
]
