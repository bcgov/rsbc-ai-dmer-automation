"""Envelope-aware Service Bus consumer with idempotency and settlement.

The consumer receives raw Service Bus messages, extracts the envelope
(``messageId``/``correlationId``), binds the correlation id for the duration of
handling, and enforces idempotency on ``messageId`` before invoking the handler.

Settlement:
- handler success               -> ``complete`` the message,
- handler raises                -> ``dead_letter`` the message (native DLQ),
- already-processed ``messageId`` -> ``complete`` without invoking the handler.

The underlying Azure ``ServiceBusReceiver`` is injected so this logic is testable
with a fake receiver.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from ..telemetry import correlation_context, get_logger
from .idempotency import IdempotencyStore, InMemoryIdempotencyStore

_log = get_logger(__name__)


class _Receiver(Protocol):
    """Subset of ``azure.servicebus.ServiceBusReceiver`` this consumer uses."""

    def complete_message(self, message: Any) -> None: ...

    def dead_letter_message(
        self,
        message: Any,
        *,
        reason: str | None = ...,
        error_description: str | None = ...,
    ) -> None: ...


def _message_body(message: Any) -> bytes:
    """Return the message body as bytes across SDK body representations."""
    body = message.body if hasattr(message, "body") else message
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    # azure-servicebus returns a generator of byte chunks for AMQP data bodies
    try:
        return b"".join(bytes(chunk) for chunk in body)
    except TypeError:
        return str(body).encode("utf-8")


def _envelope(message: Any) -> dict[str, Any]:
    """Parse the JSON envelope from a message body."""
    return json.loads(_message_body(message).decode("utf-8"))


class ServiceBusConsumer:
    """Consumes messages, enforcing correlation propagation and idempotency.

    Parameters
    ----------
    receiver:
        An Azure ``ServiceBusReceiver`` (or compatible) for a single queue.
    idempotency_store:
        Records completed ``messageId``s (defaults to in-memory).
    """

    def __init__(
        self,
        receiver: _Receiver,
        *,
        idempotency_store: IdempotencyStore | None = None,
    ) -> None:
        self._receiver = receiver
        self._idempotency = idempotency_store or InMemoryIdempotencyStore()

    def handle(self, message: Any, handler: Callable[[dict[str, Any]], None]) -> bool:
        """Process a single message end-to-end.

        Returns True if the handler ran, False if it was skipped as a duplicate.
        On handler failure the message is dead-lettered and the error re-raised
        to the caller for logging/metrics.
        """
        envelope = _envelope(message)
        message_id = envelope.get("messageId")
        correlation_id = envelope.get("correlationId")
        if not message_id:
            self._receiver.dead_letter_message(
                message,
                reason="MissingMessageId",
                error_description="Envelope has no messageId",
            )
            return False

        with correlation_context(correlation_id):
            if self._idempotency.is_processed(message_id):
                _log.info(
                    "duplicate message; completing without reprocessing",
                    extra={"message_id": message_id},
                )
                self._receiver.complete_message(message)
                return False
            try:
                handler(envelope)
            except Exception as exc:  # route to DLQ, then re-raise
                _log.error(
                    "handler failed; dead-lettering",
                    extra={"message_id": message_id, "error": str(exc)},
                )
                self._receiver.dead_letter_message(
                    message,
                    reason="HandlerError",
                    error_description=type(exc).__name__,
                )
                raise
            self._idempotency.mark_processed(message_id)
            self._receiver.complete_message(message)
            return True
