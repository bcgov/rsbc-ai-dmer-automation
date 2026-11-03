"""DLQ Drain: reads every pipeline queue's dead-letter sub-queue.

See docs/development/stages/10-dlq-drain.md. Per message:
:func:`dmer_common.db.dlq_drain.drain_dead_letter` classifies and records it
in one transaction; then, for a redrive, a fresh copy is sent to the main
queue (``attempt`` + 1, a new message id, scheduled after a backoff); then
the dead letter is completed. A failure anywhere leaves the dead letter in
place (abandoned) for the next run -- the record is idempotent on the
message id, and the redrive copy's message id is deterministic, so a retry
of a half-finished message neither records nor redrives it twice.

Service Bus calls are blocking (the sync SDK, like the driver
orchestrator's); they run off the event loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from dmer_common.db.dlq_drain import DeadLetter, DrainAction, drain_dead_letter
from dmer_common.db.status import PipelineStage, PipelineStatus
from dmer_common.telemetry import get_logger
from sqlalchemy.ext.asyncio import AsyncEngine

from ._runtime import run_sync

_log = get_logger(__name__)


@dataclass(frozen=True)
class DrainSource:
    """One pipeline queue whose dead letters the drain reads."""

    queue: str
    stage: PipelineStage
    entry_status: PipelineStatus | None
    routes_document: bool = True
    # The driver-decision queue is session-enabled (SessionId = driver_key).
    session_key: str | None = None
    # Whether a redrive counts as a delivery attempt. Not on driver-decision,
    # where ``attempt`` counts the Mercury re-checks (driver_orchestrator.recheck).
    bump_attempt: bool = True


def pipeline_sources(
    *, ingest: str, raw: str, extracted: str, driver_decision: str
) -> list[DrainSource]:
    """The four pipeline queues (architecture 4.6.3) with their stages."""
    return [
        DrainSource(ingest, PipelineStage.INGEST, PipelineStatus.RECEIVED),
        DrainSource(raw, PipelineStage.EXTRACT, PipelineStatus.DOWNLOADED),
        DrainSource(extracted, PipelineStage.NORMALIZE, PipelineStatus.EXTRACTED),
        # A driver-level signal: a redrive re-signals the driver and touches
        # no document; anything else is recorded for a person.
        DrainSource(
            driver_decision,
            PipelineStage.DECISION,
            None,
            routes_document=False,
            session_key="driverKey",
            bump_attempt=False,
        ),
    ]


@dataclass(frozen=True)
class DrainSettings:
    batch_size: int = 20  # dead letters per queue per run
    max_redrives: int = 3
    redrive_backoff: timedelta = timedelta(minutes=5)  # doubled per redrive
    receive_wait_seconds: int = 5


@dataclass
class DrainSummary:
    received: int = 0
    actions: dict[str, int] = field(default_factory=dict)
    left_in_dlq: int = 0  # failed this run; retried next run

    def count(self, action: DrainAction) -> None:
        self.actions[action.value] = self.actions.get(action.value, 0) + 1


class _Receiver(Protocol):
    def receive_messages(self, max_message_count: int, max_wait_time: int) -> list: ...
    def complete_message(self, message: Any) -> None: ...
    def abandon_message(self, message: Any) -> None: ...


class _Sender(Protocol):
    def send_messages(self, message: Any) -> None: ...


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _body(message: Any) -> dict[str, Any]:
    """The envelope (camelCase JSON); {} when it isn't JSON."""
    raw = message.body
    if not isinstance(raw, (bytes, bytearray, str)):
        raw = b"".join(bytes(part) for part in raw)
    try:
        body = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def dead_letter_of(source: DrainSource, message: Any) -> tuple[DeadLetter, dict]:
    body = _body(message)
    document_id = body.get("documentId") or _text(message.correlation_id)
    return (
        DeadLetter(
            queue=source.queue,
            stage=source.stage.value,
            message_id=_text(message.message_id) or "",
            document_id=document_id,
            reason=_text(message.dead_letter_reason),
            description=_text(message.dead_letter_error_description),
            entry_status=source.entry_status,
            routes_document=source.routes_document,
        ),
        body,
    )


def redrive_body(
    body: dict[str, Any], message_id: str, redrive: int, *, bump_attempt: bool = True
) -> dict:
    """The re-sent envelope: same content, a new message id (deterministic
    per redrive, so re-sending it twice is deduplicated downstream) and, for
    document queues, ``attempt`` + 1."""
    copy = {
        **body,
        "messageId": f"{body.get('messageId') or message_id}:redrive{redrive}",
    }
    if bump_attempt:
        copy["attempt"] = int(body.get("attempt") or 1) + 1
    return copy


def _default_message_factory(body: str, **kwargs: Any) -> Any:
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(body, **kwargs)


async def drain_source(
    engine: AsyncEngine,
    source: DrainSource,
    receiver: _Receiver,
    sender: _Sender,
    *,
    settings: DrainSettings,
    clock: Callable[[], datetime],
    message_factory: Callable[..., Any] = _default_message_factory,
    summary: DrainSummary | None = None,
) -> DrainSummary:
    """Drain up to ``settings.batch_size`` dead letters from one queue."""
    summary = summary or DrainSummary()
    messages: Iterable = await run_sync(
        receiver.receive_messages,
        max_message_count=settings.batch_size,
        max_wait_time=settings.receive_wait_seconds,
    )
    for message in messages:
        summary.received += 1
        dead_letter, body = dead_letter_of(source, message)
        try:
            now = clock()
            result = await drain_dead_letter(
                engine, dead_letter, max_redrives=settings.max_redrives, now=now
            )
            if result.action is DrainAction.REDRIVEN:
                attempt = result.redrive_attempt
                copy = redrive_body(
                    body,
                    dead_letter.message_id,
                    attempt,
                    bump_attempt=source.bump_attempt,
                )
                session_id = (
                    body.get(source.session_key) if source.session_key else None
                )
                await run_sync(
                    sender.send_messages,
                    message_factory(
                        json.dumps(copy),
                        message_id=copy["messageId"],
                        correlation_id=dead_letter.document_id,
                        session_id=session_id,
                        scheduled_enqueue_time_utc=now
                        + settings.redrive_backoff * (2 ** (attempt - 1)),
                    ),
                )
            await run_sync(receiver.complete_message, message)
            summary.count(result.action)
        except Exception:  # noqa: BLE001 - one bad message doesn't stop the run
            summary.left_in_dlq += 1
            _log.exception(
                "dead letter not drained; left for the next run",
                extra={
                    "queue": source.queue,
                    "dlq_message_id": dead_letter.message_id,
                    "document_id": dead_letter.document_id,
                },
            )
            try:
                await run_sync(receiver.abandon_message, message)
            except Exception:  # noqa: BLE001 - the lock expires on its own
                _log.warning(
                    "could not abandon the dead letter; its lock will expire",
                    extra={"dlq_message_id": dead_letter.message_id},
                )
    return summary
