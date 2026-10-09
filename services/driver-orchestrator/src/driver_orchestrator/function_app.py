"""Driver Orchestration: the session-triggered ``driver-decision`` function.

See docs/development/stages/06-driver-orchestration.md. Every document's
Signal Driver step (Document Orchestration, PR #22) publishes one
``driver-decision`` message with ``SessionId = driver_key``. This function
consumes the queue with sessions enabled, so the Functions host processes a
driver's messages **one at a time, each to completion** -- a signal that
arrives while the driver is being evaluated waits its turn instead of being
dropped. Different drivers run in parallel.

Design note (pending architect approval): the architecture describes a
Durable orchestration per driver. A Durable starter only holds the session
lock while it *starts* the orchestration, so a second signal for the same
driver can arrive while the first orchestration runs (or after it ended
WAITING) and be lost. Evaluating inside the session-triggered function closes
that gap. All the logic lives in :func:`evaluate.evaluate_driver`, which a
Durable design would call as an activity unchanged.

Failure handling: a malformed message, a busy driver lock or a database
failure raises, so Service Bus redelivers the message (and dead-letters it
after the queue's max delivery count). A Mercury outage does not raise: the
evaluation is recorded as WAITING and re-run by the next signal or the
Reconciliation Sweeper.
"""

from __future__ import annotations

from uuid import UUID

import azure.functions as func
from dmer_common.dto import DriverDecisionMessage
from dmer_common.mercury_client import MercuryClient
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine
from .evaluate import Evaluation, evaluate_driver

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


async def handle_driver_signal(
    body: bytes,
    *,
    engine_factory=get_async_engine,
    mercury: MercuryClient | None = None,
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
