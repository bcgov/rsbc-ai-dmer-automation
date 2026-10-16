"""Reliability components: the Outbox Publisher timer function.

See docs/development/stages/09-reliability-components.md (architecture v2.1
4.6.1). Post-Processing writes ``mercury_outbox`` rows in the same
transaction as each decision; this function drains them to Mercury about
once a minute (``OUTBOX_PUBLISHER_SCHEDULE``). All the logic is in
:func:`dmer_common.db.outbox_publisher.publish_due`; this module only reads
the settings and wires the database engine and the Mercury client.

The Reconciliation Sweeper and the DLQ Drain will join this app as further
timer functions (a later branch).

Turn the publisher off without a deploy with the app setting
``AzureWebJobs.OutboxPublisher.Disabled = true``.
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
from dmer_common.mercury_client import MercuryClient
from dmer_common.telemetry import get_logger

from ._runtime import get_async_engine

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
