"""Reliability components (architecture v2.1 4.6): timer functions.

- **OutboxPublisher** (docs/development/stages/09-reliability-components.md,
  4.6.1): Post-Processing writes ``mercury_outbox`` rows in the same
  transaction as each decision; this drains them to Mercury about once a
  minute (``OUTBOX_PUBLISHER_SCHEDULE``). Logic:
  :func:`dmer_common.db.outbox_publisher.publish_due`.
- **DlqDrain** (stages/10-dlq-drain.md, 4.6.3): every pipeline queue's
  dead-letter sub-queue, every few minutes (``DLQ_DRAIN_SCHEDULE``): a
  failure a retry could fix is re-sent (bounded); a permanent one gets the
  fallback IN; anything else goes to MANUAL_REVIEW. Logic:
  :mod:`reliability.dlq_drain` over :func:`dmer_common.db.dlq_drain.drain_dead_letter`.

- **ReconciliationSweeper** (09-reliability-components.md, 4.6.2): every
  ~15 minutes (``SWEEPER_SCHEDULE``): documents stalled in a stage past its
  SLA are re-published (bounded, then MANUAL_REVIEW), stuck WAITING driver
  evaluations are re-signalled, and the outbox backlog is logged for the
  alert. Logic: :mod:`reliability.sweeper` over :mod:`dmer_common.db.sweeper`.

- **DailyReconciliation** (09-reliability-components.md "Daily
  reconciliation report"): once a day (``RECONCILIATION_SCHEDULE``) every
  document in Mercury's DPS queue is checked against our tables; the report
  goes to ``RECONCILIATION_CONTAINER`` and its summary to the logs. Logic:
  :mod:`reliability.reconciliation` over :mod:`dmer_common.db.reconciliation`.

This module only reads the settings and wires the database engine, the
Mercury client and the Service Bus client. Turn a function off without a
deploy with ``AzureWebJobs.<FunctionName>.Disabled = true``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import azure.functions as func
from dmer_common.db.outbox_publisher import (
    PublisherSettings,
    PublishSummary,
    publish_due,
)
from dmer_common.db.processing_error import FailureCategory
from dmer_common.db.status import PipelineStage
from dmer_common.db.sweeper import SweeperSettings, SweepQueue
from dmer_common.mercury_client import MercuryClient
from dmer_common.telemetry import get_logger

from ._runtime import get_async_engine
from .dlq_drain import (
    DrainSettings,
    DrainSource,
    DrainSummary,
    drain_source,
    pipeline_sources,
)
from .reconciliation import ReportSettings, run_report
from .sweeper import SweepSummary, run_sweep

_log = get_logger(__name__)

app = func.FunctionApp()


def _int(name: str, default: int) -> int:
    value = os.environ.get(name, "").strip()
    return int(value) if value else default


def publisher_settings() -> PublisherSettings:
    """The publisher's tuning from app settings (defaults: architecture 4.6.1)."""
    return PublisherSettings(
        batch_size=_int("OUTBOX_BATCH_SIZE", 20),
        max_attempts=_int("OUTBOX_MAX_ATTEMPTS", 10),
        backoff_initial=timedelta(seconds=_int("OUTBOX_BACKOFF_INITIAL_SECONDS", 60)),
        backoff_max=timedelta(seconds=_int("OUTBOX_BACKOFF_MAX_SECONDS", 3600)),
        lease=timedelta(seconds=_int("OUTBOX_LEASE_SECONDS", 300)),
    )


def _now() -> datetime:
    return datetime.now(UTC)


async def run_publisher(
    *, engine_factory=get_async_engine, client: MercuryClient | None = None
) -> PublishSummary:
    """One publisher run with a fresh engine (disposed afterwards)."""
    engine = await engine_factory()
    try:
        return await publish_due(
            engine,
            client or MercuryClient(),
            settings=publisher_settings(),
            clock=_now,
        )
    finally:
        await engine.dispose()


def drain_settings() -> DrainSettings:
    """The DLQ Drain's tuning from app settings (stages/10-dlq-drain.md)."""
    enabled = {
        c.strip()
        for c in os.environ.get(
            "FALLBACK_ENABLED_CATEGORIES", FailureCategory.PERMANENT_BUSINESS.value
        ).split(",")
        if c.strip()
    }
    # A deliberate gate (10-dlq-drain.md): no other category may be enabled
    # to produce a fallback decision without a documented business rule.
    if enabled != {FailureCategory.PERMANENT_BUSINESS.value}:
        raise ValueError("FALLBACK_ENABLED_CATEGORIES supports only PERMANENT_BUSINESS")
    return DrainSettings(
        batch_size=_int("DLQ_DRAIN_BATCH_SIZE", 20),
        max_redrives=_int("DLQ_REDRIVE_MAX_ATTEMPTS", 3),
        redrive_backoff=timedelta(seconds=_int("DLQ_REDRIVE_BACKOFF_SECONDS", 300)),
    )


def drain_sources() -> list[DrainSource]:
    return pipeline_sources(
        ingest=os.environ.get("DMER_INGEST_QUEUE", "dmer-ingest"),
        raw=os.environ.get("DMER_RAW_QUEUE", "dmer-raw"),
        extracted=os.environ.get("DMER_EXTRACTED_QUEUE", "dmer-extracted"),
        driver_decision=os.environ.get("DRIVER_DECISION_QUEUE", "driver-decision"),
    )


def _service_bus_client():
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    return ServiceBusClient(
        os.environ["SERVICE_BUS_NAMESPACE_FQDN"], DefaultAzureCredential()
    )


async def run_drain(
    *, engine_factory=get_async_engine, client_factory=_service_bus_client
) -> DrainSummary:
    """One drain run over every pipeline queue's dead-letter sub-queue."""
    from azure.servicebus import ServiceBusSubQueue

    settings = drain_settings()
    summary = DrainSummary()
    engine = await engine_factory()
    try:
        with client_factory() as client:
            for source in drain_sources():
                with client.get_queue_receiver(
                    source.queue, sub_queue=ServiceBusSubQueue.DEAD_LETTER
                ) as receiver, client.get_queue_sender(source.queue) as sender:
                    await drain_source(
                        engine,
                        source,
                        receiver,
                        sender,
                        settings=settings,
                        clock=_now,
                        summary=summary,
                    )
    finally:
        await engine.dispose()
    if summary.received:
        _log.warning(
            "dlq drain run",
            extra={
                "received": summary.received,
                "left_in_dlq": summary.left_in_dlq,
                **{f"action_{k.lower()}": v for k, v in summary.actions.items()},
            },
        )
    return summary


@app.function_name(name="DlqDrain")
@app.timer_trigger(
    arg_name="timer",
    schedule="%DLQ_DRAIN_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
async def dlq_drain(timer: func.TimerRequest) -> None:
    if timer.past_due:
        _log.warning("dlq drain run is past due")
    await run_drain()


def _minutes(name: str, default: int) -> timedelta:
    return timedelta(minutes=_int(name, default))


def sweeper_settings() -> SweeperSettings:
    """The sweeper's thresholds from app settings (``STAGE_SLA_*``: question
    I-14 -- the defaults are placeholders until it is answered)."""
    return SweeperSettings(
        stage_sla={
            PipelineStage.INGEST: _minutes("STAGE_SLA_INGEST_MINUTES", 30),
            PipelineStage.EXTRACT: _minutes("STAGE_SLA_EXTRACT_MINUTES", 30),
            PipelineStage.NORMALIZE: _minutes("STAGE_SLA_NORMALIZE_MINUTES", 30),
        },
        driver_sla=_minutes("STAGE_SLA_DRIVER_MINUTES", 30),
        max_republish=_int("SWEEPER_MAX_REPUBLISH", 3),
        batch_size=_int("SWEEPER_BATCH_SIZE", 50),
        outbox_pending_sla=_minutes("OUTBOX_BACKLOG_SLA_MINUTES", 60),
        outbox_attempt_alert=_int("OUTBOX_ATTEMPT_ALERT", 5),
    )


def _queue_names() -> dict[SweepQueue, str]:
    sources = {s.stage: s.queue for s in drain_sources()}
    return {
        SweepQueue.INGEST: sources[PipelineStage.INGEST],
        SweepQueue.RAW: sources[PipelineStage.EXTRACT],
        SweepQueue.EXTRACTED: sources[PipelineStage.NORMALIZE],
        SweepQueue.DRIVER_DECISION: sources[PipelineStage.DECISION],
    }


async def run_sweeper(
    *, engine_factory=get_async_engine, client_factory=_service_bus_client
) -> SweepSummary:
    """One sweeper run (a fresh engine and Service Bus client)."""
    settings = sweeper_settings()
    queues = _queue_names()
    engine = await engine_factory()
    try:
        with client_factory() as client:

            def send(queue: SweepQueue, message) -> None:
                with client.get_queue_sender(queues[queue]) as sender:
                    sender.send_messages(message)

            return await run_sweep(engine, send, settings=settings, clock=_now)
    finally:
        await engine.dispose()


@app.function_name(name="ReconciliationSweeper")
@app.timer_trigger(
    arg_name="timer",
    schedule="%SWEEPER_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
async def reconciliation_sweeper(timer: func.TimerRequest) -> None:
    if timer.past_due:
        _log.warning("sweeper run is past due")
    await run_sweeper()


def report_settings() -> ReportSettings:
    """The reconciliation report's settings."""
    return ReportSettings(
        queue=os.environ.get("MERCURY_QUEUE", "BOTH"),
        page_size=_int("MERCURY_PAGE_SIZE", 50),
        max_pages=_int("RECONCILIATION_MAX_PAGES", 2000),
        receive_grace=timedelta(hours=_int("RECONCILIATION_RECEIVE_GRACE_HOURS", 2)),
        overdue=timedelta(hours=_int("RECONCILIATION_OVERDUE_HOURS", 24)),
    )


def _report_saver():
    """Saves a report blob to RECONCILIATION_CONTAINER on the runtime storage."""
    from azure.core.exceptions import ResourceExistsError
    from azure.storage.blob import BlobServiceClient

    service = BlobServiceClient.from_connection_string(
        os.environ["AzureWebJobsStorage"]
    )
    container = service.get_container_client(
        os.environ.get("RECONCILIATION_CONTAINER", "reconciliation-reports")
    )
    try:
        container.create_container()
    except ResourceExistsError:
        pass

    def save(name: str, body: bytes) -> None:
        container.upload_blob(name, body, overwrite=True)

    return save


async def run_reconciliation(
    *, engine_factory=get_async_engine, client=None, saver_factory=_report_saver
) -> dict:
    """One daily reconciliation report (a fresh engine, disposed afterwards)."""
    engine = await engine_factory()
    try:
        return await run_report(
            engine,
            client or MercuryClient(),
            saver_factory(),
            settings=report_settings(),
            clock=_now,
        )
    finally:
        await engine.dispose()


@app.function_name(name="DailyReconciliation")
@app.timer_trigger(
    arg_name="timer",
    schedule="%RECONCILIATION_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
async def daily_reconciliation(timer: func.TimerRequest) -> None:
    if timer.past_due:
        _log.warning("daily reconciliation is past due")
    await run_reconciliation()


@app.function_name(name="OutboxPublisher")
@app.timer_trigger(
    arg_name="timer",
    schedule="%OUTBOX_PUBLISHER_SCHEDULE%",
    run_on_startup=False,
    use_monitor=True,
)
async def outbox_publisher(timer: func.TimerRequest) -> None:
    if timer.past_due:
        _log.warning("outbox publisher run is past due")
    await run_publisher()
