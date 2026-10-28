"""Reconciliation Sweeper: one run of the three sweeps, then the messages.

See docs/development/stages/09-reliability-components.md (4.6.2). The
queries and their records are :mod:`dmer_common.db.sweeper`; this module
publishes the re-publishes they return, each with a new message id
(``<document_guid>:sweep<n>`` -- the queues' duplicate detection would drop a
reused one), and logs the run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from dmer_common.db.sweeper import (
    OutboxBacklog,
    Republish,
    SweeperSettings,
    SweepQueue,
    outbox_backlog,
    sweep_stale_evaluations,
    sweep_stalled_documents,
)
from dmer_common.dto.messages import PIPELINE_SCHEMA_VERSION
from dmer_common.telemetry import get_logger
from sqlalchemy.ext.asyncio import AsyncEngine

from ._runtime import run_sync

_log = get_logger(__name__)


@dataclass
class SweepSummary:
    republished: dict[str, int] = field(default_factory=dict)
    publish_failures: int = 0
    manual_review: int = 0
    outbox: OutboxBacklog | None = None


def envelope(item: Republish, now: datetime) -> dict[str, Any]:
    """The pipeline envelope (camelCase, message-contracts.md) for a re-publish."""
    return {
        "schemaVersion": PIPELINE_SCHEMA_VERSION,
        "messageId": f"{item.document_guid}:sweep{item.sweep}",
        "documentId": item.document_id,
        "documentGuid": item.document_guid,
        "driverKey": item.driver_key,
        "blobUrl": item.blob_url,
        "attempt": item.sweep + 1,
        "enqueuedAt": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _default_message_factory(body: str, **kwargs: Any) -> Any:
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(body, **kwargs)


async def run_sweep(
    engine: AsyncEngine,
    send: Callable[[SweepQueue, Any], None],
    *,
    settings: SweeperSettings,
    clock: Callable[[], datetime],
    message_factory: Callable[..., Any] = _default_message_factory,
) -> SweepSummary:
    """One sweep: records first (committed), then the messages.

    *send(queue, message)* puts one message on the named pipeline queue
    (blocking; run off the event loop). A failed send is logged and left:
    the document's ``updated_at`` has moved on, so the next sweep after the
    SLA tries again.
    """
    now = clock()
    summary = SweepSummary()
    documents = await sweep_stalled_documents(engine, settings=settings, now=now)
    signals = await sweep_stale_evaluations(engine, settings=settings, now=now)
    summary.manual_review = len(documents.manual_review)
    for item in [*documents.republish, *signals]:
        body = envelope(item, now)
        session_id = (
            item.driver_key if item.queue is SweepQueue.DRIVER_DECISION else None
        )
        try:
            await run_sync(
                send,
                item.queue,
                message_factory(
                    json.dumps(body),
                    message_id=body["messageId"],
                    correlation_id=item.document_id,
                    session_id=session_id,
                ),
            )
        except Exception:  # noqa: BLE001 - one failed send doesn't stop the run
            summary.publish_failures += 1
            _log.exception(
                "sweeper re-publish failed; the next sweep retries",
                extra={"document_id": item.document_id, "queue": item.queue.value},
            )
            continue
        key = item.queue.value
        summary.republished[key] = summary.republished.get(key, 0) + 1
    summary.outbox = await outbox_backlog(engine, settings=settings, now=now)
    if summary.republished or summary.manual_review or summary.publish_failures:
        _log.warning(
            "sweeper run",
            extra={
                "manual_review": summary.manual_review,
                "publish_failures": summary.publish_failures,
                **{f"republished_{k}": v for k, v in summary.republished.items()},
            },
        )
    return summary
