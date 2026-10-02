"""Signal Driver Durable activity: publish ``driver-decision`` for a document.

See docs/development/stages/03-document-orchestration.md (orchestration flow,
step 5) and message-contracts.md. The last step of the Document Orchestration:
the document's rules are applied, so its driver's batch may now be ready for a
decision. The message goes to the session-enabled ``driver-decision`` queue
with ``SessionId = driver_key`` -- one writer per driver -- and the document
moves ``RULES_APPLIED`` -> ``AWAITING_DRIVER_COMPLETION``.

Idempotent: the ``message_id`` is derived from the document
(``event_message_id("driver-decision", document_id)``), so a retry that
publishes again sends the same event, which duplicate detection and the
consumer's idempotency recognise; a document already signalled is not
published again.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from urllib.parse import urlparse
from uuid import UUID

import azure.durable_functions as df
from dmer_common.db.document_routing import (
    load_signal_state,
    mark_awaiting_driver_completion,
)
from dmer_common.dto import (
    DRIVER_DECISION_EVENT,
    DriverDecisionMessage,
    event_message_id,
)
from dmer_common.messaging import ServiceBusPublisher
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine as _get_async_engine
from ._runtime import poison_as_result
from ._runtime import run_sync as _run_sync

_log = get_logger(__name__)

ACTIVITY_NAME = "SignalDriver"

# Already signalled (or further on): nothing to publish.
_SIGNALLED = frozenset(
    {"AWAITING_DRIVER_COMPLETION", "DECIDED", "POSTING", "COMPLETED"}
)


class SignalDriverValidationError(ValueError):
    """Poison: bad input, or a document that can't be signalled (no driver,
    rules not applied)."""


class SignalDriverActivityError(RuntimeError):
    """Operational failure; the document orchestrator owns bounded retries."""


def _input(payload: dict) -> tuple[str, str]:
    if not isinstance(payload, dict):
        raise SignalDriverValidationError("Signal Driver input must be an object")
    try:
        document_id = str(UUID(str(payload.get("document_id"))))
    except ValueError:
        raise SignalDriverValidationError(
            "Signal Driver input requires a document UUID"
        ) from None
    blob_url = payload.get("normalized_blob_url")
    parsed = urlparse(blob_url if isinstance(blob_url, str) else "")
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise SignalDriverValidationError(
            "Signal Driver input requires a normalized blob URL"
        )
    return document_id, blob_url


def publish_driver_decision(message: DriverDecisionMessage) -> None:
    """Send *message* to ``DRIVER_DECISION_QUEUE`` with ``SessionId = driver_key``,
    authenticating with Managed Identity (blocking; run in a thread)."""
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient

    credential = DefaultAzureCredential()
    try:
        with ServiceBusClient(
            os.environ["SERVICE_BUS_NAMESPACE_FQDN"], credential
        ) as client, client.get_queue_sender(
            os.environ.get("DRIVER_DECISION_QUEUE", "driver-decision")
        ) as sender:
            ServiceBusPublisher(sender).publish(message, session_id=message.driver_key)
    finally:
        credential.close()


async def signal_driver_activity(payload: dict) -> dict:
    """Publish ``driver-decision`` for the document and mark it awaiting its driver."""
    document_id, normalized_blob_url = _input(payload)
    engine = None
    with document_id_context(document_id):
        try:
            engine = await _get_async_engine()
            state = await load_signal_state(engine, document_id)
            if state is None:
                raise SignalDriverValidationError("Document not found")
            if state.pipeline_status in _SIGNALLED:
                _log.info("signal driver: already signalled")
                return {"published": False}
            if state.pipeline_status != "RULES_APPLIED" or not state.driver_key:
                raise SignalDriverValidationError(
                    "Document is not ready to signal its driver"
                )
            message = DriverDecisionMessage(
                message_id=event_message_id(DRIVER_DECISION_EVENT, document_id),
                document_id=document_id,
                document_guid=state.document_guid,
                driver_key=state.driver_key,
                blob_url=normalized_blob_url,
                enqueued_at=datetime.now(UTC),
            )
            await _run_sync(publish_driver_decision, message)
            moved = await mark_awaiting_driver_completion(
                engine, document_id, now=datetime.now(UTC)
            )
            _log.info(
                "signal driver: published",
                extra={
                    "message_id": message.message_id,
                    "driver_key": state.driver_key,
                    "status_updated": moved,
                },
            )
            return {"published": True, "message_id": message.message_id}
        except SignalDriverValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize before Durable serialization
            _log.error(
                "signal driver: operational failure",
                extra={"error_code": type(exc).__name__},
            )
            raise SignalDriverActivityError(
                "Signal Driver dependency or persistence failure"
            ) from None
        finally:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:  # noqa: BLE001 - cleanup must not replace a result
                    _log.error("signal driver: unable to dispose database engine")


POISON = (SignalDriverValidationError,)


def register(app: df.DFApp) -> None:
    """Register the Signal Driver activity on *app* (called once from
    function_app.py). Poison errors come back as a result, not a retry."""

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _signal_driver(payload: dict) -> dict:
        return await poison_as_result(signal_driver_activity(payload), POISON)
