"""Driver Orchestration: the session-triggered ``driver-decision`` function.

See docs/development/stages/06-driver-orchestration.md. Every document's
Signal Driver step (Document Orchestration, PR #22) publishes one
``driver-decision`` message with ``SessionId = driver_key``. This function
consumes the queue with sessions enabled, so the Functions host processes a
driver's messages **one at a time, each to completion** -- a signal that
arrives while the driver is being evaluated waits its turn instead of being
dropped. Different drivers run in parallel.

Design decision (ADR-0003, approved by the architect): the architecture describes a
Durable orchestration per driver. A Durable starter only holds the session
lock while it *starts* the orchestration, so a second signal for the same
driver can arrive while the first orchestration runs (or after it ended
WAITING) and be lost. Evaluating inside the session-triggered function closes
that gap. All the logic lives in :func:`evaluate.evaluate_driver`, which a
Durable design would call as an activity unchanged.

Failure handling: a malformed message, a busy driver lock or a database
failure raises, so Service Bus redelivers the message (and dead-letters it
after the queue's max delivery count). A Mercury outage does not raise: the
evaluation is recorded as WAITING and this function schedules its own
re-check of the driver with backoff (see :mod:`.recheck`), so the driver is
evaluated again even if none of its DMERs signals. If Mercury still can't
verify the batch on the last re-check, its undecided DMERs go to
MANUAL_REVIEW with a fallback IN outcome and a note saying why.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import azure.functions as func
from dmer_common.dto import DriverDecisionMessage
from dmer_common.mercury_client import MercuryClient
from dmer_common.messaging import ServiceBusPublisher
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine, run_sync
from .decision import WaitReason
from .evaluate import Evaluation, Result, evaluate_driver
from .recheck import recheck_delay, recheck_message

_log = get_logger(__name__)

app = func.FunctionApp()


def parse_signal(body: bytes) -> DriverDecisionMessage:
    """Parse and validate one ``driver-decision`` envelope (camelCase JSON).

    Raises ``ValueError`` on a malformed message or one without a valid
    ``driverKey`` -- left to propagate so the message is redelivered and,
    after the max delivery count, dead-lettered.
    """
    # pydantic's ValidationError is a ValueError.
    message = DriverDecisionMessage.model_validate_json(body)
    try:
        UUID(message.driver_key)
    except (TypeError, ValueError):
        raise ValueError("driver-decision message requires a driverKey UUID") from None
    return message


def publish_recheck(message: DriverDecisionMessage, scheduled_for: datetime) -> None:
    """Schedule *message* on ``driver-decision`` for *scheduled_for* (UTC),
    ``SessionId = driver_key``, authenticating with Managed Identity
    (blocking; run in a thread)."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    credential = DefaultAzureCredential()
    try:
        with ServiceBusClient(
            os.environ["SERVICE_BUS_NAMESPACE_FQDN"], credential
        ) as client, client.get_queue_sender(
            os.environ.get("DRIVER_DECISION_QUEUE", "driver-decision")
        ) as sender:
            ServiceBusPublisher(sender).publish(
                message,
                session_id=message.driver_key,
                scheduled_enqueue_time=scheduled_for,
            )
    finally:
        credential.close()


async def schedule_recheck_if_unverified(
    message: DriverDecisionMessage,
    evaluation: Evaluation,
    *,
    publish: Callable[[DriverDecisionMessage, datetime], None] = publish_recheck,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> datetime | None:
    """If Mercury could not verify the batch, schedule the next re-check;
    returns when it is due, or None (not needed, used up, or failed).

    Never raises: the WAITING state is already recorded, so a failed schedule
    leaves the driver to its next signal or the Reconciliation Sweeper
    (raising would only make Service Bus retry at once, against the same
    Mercury outage).
    """
    if (
        evaluation.result is not Result.WAITING
        or evaluation.wait_reason is not WaitReason.MERCURY_UNVERIFIED
    ):
        return None
    delay = recheck_delay(message.attempt)
    if delay is None:
        _log.error(
            "driver decision: Mercury still unverified; re-checks used up",
            extra={
                "driver_evaluation_id": evaluation.evaluation_id,
                "attempt": message.attempt,
            },
        )
        return None
    now = clock()
    due = now + delay
    recheck = recheck_message(message, now=now)
    try:
        await run_sync(publish, recheck, due)
    except Exception as exc:  # noqa: BLE001 - see the docstring
        _log.error(
            "driver decision: re-check could not be scheduled",
            extra={"error_code": type(exc).__name__, "attempt": recheck.attempt},
        )
        return None
    _log.info(
        "driver decision: re-check scheduled",
        extra={
            "driver_evaluation_id": evaluation.evaluation_id,
            "attempt": recheck.attempt,
            "due": due.isoformat(),
        },
    )
    return due


async def handle_driver_signal(
    body: bytes,
    *,
    engine_factory=get_async_engine,
    mercury: MercuryClient | None = None,
    publish: Callable[[DriverDecisionMessage, datetime], None] = publish_recheck,
) -> Evaluation:
    """Evaluate the signalled driver's batch once.

    Bare function (no Functions-host decorators) so a unit test can call it
    with fakes; the registered trigger below only forwards the body.
    """
    message = parse_signal(body)
    driver_key = str(UUID(message.driver_key))
    mercury = mercury or MercuryClient()
    settings = mercury.driver_settings
    with document_id_context(message.document_id):
        _log.info("driver decision: signal received")
        engine = await engine_factory()
        try:
            evaluation = await evaluate_driver(
                engine,
                driver_key,
                mercury_lookup=mercury.get_driver_by_licence,
                counted_types=settings.counted_document_types,
                uncounted_statuses=settings.uncounted_document_statuses,
                # No re-check left after this one: if Mercury still can't
                # verify, send the batch to manual review instead of waiting.
                final_attempt=recheck_delay(message.attempt) is None,
            )
        finally:
            try:
                await engine.dispose()
            except Exception:  # noqa: BLE001 - cleanup must not replace a result
                _log.error("driver decision: unable to dispose database engine")
        _log.info(
            "driver decision: evaluated",
            extra={
                "result": evaluation.result,
                "driver_evaluation_id": evaluation.evaluation_id,
                "wait_reason": evaluation.wait_reason,
                "decided_count": evaluation.decided_count,
            },
        )
        await schedule_recheck_if_unverified(message, evaluation, publish=publish)
        return evaluation


@app.function_name(name="DriverDecision")
@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%DRIVER_DECISION_QUEUE%",
    connection="ServiceBusConnection",
    is_sessions_enabled=True,
)
async def _driver_decision(msg: func.ServiceBusMessage) -> None:
    await handle_driver_signal(msg.get_body())
