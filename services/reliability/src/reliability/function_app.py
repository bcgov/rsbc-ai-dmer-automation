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
