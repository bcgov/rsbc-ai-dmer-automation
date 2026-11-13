"""Envelope-aware Service Bus consumer with durable idempotency and settlement.

The consumer receives raw Service Bus messages, extracts the envelope
(``messageId``/``documentId``), binds the document id for the duration of
handling, and **claims** the message in the idempotency store before invoking
the handler (see :mod:`dmer_common.messaging.idempotency` for the semantics).

Per message:

- claim ``CLAIMED``     -> run the handler;
  - success             -> mark the claim ``COMPLETED``, then ``complete`` the
                           message;
  - handler raises      -> release the claim, then ``dead_letter`` the message
                           (it stays eligible for redrive);
- claim ``DUPLICATE``   -> already completed: ``complete`` without the handler;
- claim ``IN_PROGRESS`` -> another worker holds a live claim: ``abandon`` so
                           Service Bus redelivers it later (never complete it —
                           if that worker crashed, the message would be lost);
- the claim itself fails (store unavailable) -> the error propagates and the
  message is left unsettled; its lock expires and it is redelivered.

Dead-letter reason: a handler exception may carry ``dead_letter_reason`` and
``safe_detail`` string attributes (a classified, PII-safe failure); they become
the dead-letter ``reason`` / ``description``. Otherwise the reason is
``HandlerError`` and the description the exception type. The exception *message*
is never logged or sent, since it can contain extracted values.

The underlying Azure ``ServiceBusReceiver`` is injected so this logic is testable
with a fake receiver.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from ..telemetry import document_id_context, get_logger
from .idempotency import ClaimOutcome, IdempotencyStore

_log = get_logger(__name__)


class _Receiver(Protocol):
    """Subset of ``azure.servicebus.ServiceBusReceiver`` this consumer uses."""

    def complete_message(self, message: Any) -> None: ...

    def abandon_message(self, message: Any) -> None: ...

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


def _message_id(message: Any, envelope: dict[str, Any]) -> str | None:
    """The message's idempotency ID: the envelope's ``messageId``, else the
    broker ``MessageId`` property (some producers set only the latter)."""
    body_id = envelope.get("messageId")
    if body_id:
        return str(body_id)
    broker_id = getattr(message, "message_id", None)
    return str(broker_id) if broker_id else None


def _dead_letter_fields(exc: BaseException) -> tuple[str, str]:
    """Return ``(reason, description)`` for dead-lettering ``exc``.

    Uses the exception's ``dead_letter_reason`` / ``safe_detail`` when it
    provides them as non-empty strings; otherwise ``HandlerError`` and the
    exception type. Never the exception message.
    """
    reason = getattr(exc, "dead_letter_reason", None)
    detail = getattr(exc, "safe_detail", None)
    return (
        reason if isinstance(reason, str) and reason else "HandlerError",
        detail if isinstance(detail, str) and detail else type(exc).__name__,
    )


class ServiceBusConsumer:
    """Consumes messages, enforcing document-id propagation and idempotency.

    Parameters
    ----------
    receiver:
        An Azure ``ServiceBusReceiver`` (or compatible) for a single queue.
    idempotency_scope:
        Names this consumer in the idempotency store, e.g.
        ``di-processor/dmer-raw``. Required, so a store shared between services
        can never let one consumer's completed ``messageId`` suppress another's.
    idempotency_store:
        The durable claim store. **Required** — there is no in-memory default,
        so a deployed service can't silently lose idempotency on restart or
        across replicas. Pass ``InMemoryIdempotencyStore`` in tests only.
    """

    def __init__(
        self,
        receiver: _Receiver,
        *,
        idempotency_scope: str,
        idempotency_store: IdempotencyStore,
    ) -> None:
        if not idempotency_scope:
            raise ValueError("idempotency_scope must be a non-empty consumer name")
        self._receiver = receiver
        self._scope = idempotency_scope
        self._idempotency = idempotency_store

    def handle(self, message: Any, handler: Callable[[dict[str, Any]], None]) -> bool:
        """Process a single message end-to-end.

        Returns True if the handler ran, False if it was skipped (duplicate or in
        progress elsewhere). On handler failure the claim is released, the
        message dead-lettered, and the error re-raised to the caller.
        """
        envelope = _envelope(message)
        message_id = _message_id(message, envelope)
        if message_id and not envelope.get("messageId"):
            # Producers may set the ID only as the broker MessageId (Ingest does):
            # carry it into the envelope so the handler's model sees it.
            envelope["messageId"] = message_id
        document_id = envelope.get("documentId")
        if not message_id:
            self._receiver.dead_letter_message(
                message,
                reason="MissingMessageId",
                error_description="Envelope has no messageId",
            )
            return False

        with document_id_context(document_id):
            # Raises if the store is unavailable: the message is left unsettled
            # and redelivered after its lock expires.
            claim = self._idempotency.claim(self._scope, message_id)
            if claim.outcome is ClaimOutcome.DUPLICATE:
                _log.info(
                    "duplicate message; completing without reprocessing",
                    extra={"message_id": message_id},
                )
                self._receiver.complete_message(message)
                return False
            if claim.outcome is ClaimOutcome.IN_PROGRESS:
                _log.warning(
                    "message claimed by another worker; abandoning for redelivery",
                    extra={"message_id": message_id},
                )
                self._receiver.abandon_message(message)
                return False

            token = claim.token or ""
            try:
                handler(envelope)
            except Exception as exc:  # release, dead-letter, then re-raise
                self._release(message_id, token)
                reason, description = _dead_letter_fields(exc)
                # Never log str(exc): exception text can carry extracted PII /
                # clinical values that key-based redaction cannot catch.
                _log.error(
                    "handler failed; dead-lettering",
                    extra={
                        "message_id": message_id,
                        "error": type(exc).__name__,
                        "reason": reason,
                        "error_description": description,
                    },
                )
                self._receiver.dead_letter_message(
                    message,
                    reason=reason,
                    error_description=description,
                )
                raise
            self._complete(message_id, token)
            self._receiver.complete_message(message)
            return True

    def _complete(self, message_id: str, token: str) -> None:
        """Mark the claim COMPLETED; best-effort once the handler has succeeded.

        If this fails (or the claim was taken over), the message is still
        completed: the handler's work is done, and a later duplicate delivery is
        handled by the handler's own idempotency.
        """
        try:
            if not self._idempotency.complete(self._scope, message_id, token):
                _log.warning(
                    "idempotency claim was taken over before completion",
                    extra={"message_id": message_id},
                )
        except Exception as exc:  # noqa: BLE001 - the handler already succeeded
            _log.error(
                "failed to record idempotency completion",
                extra={"message_id": message_id, "error": type(exc).__name__},
            )

    def _release(self, message_id: str, token: str) -> None:
        """Drop the claim after a handler failure; best-effort.

        If this fails the claim simply expires with its lease, after which the
        message (e.g. redriven from the DLQ) can be claimed again.
        """
        try:
            self._idempotency.release(self._scope, message_id, token)
        except Exception as exc:  # noqa: BLE001 - don't mask the handler error
            _log.error(
                "failed to release idempotency claim",
                extra={"message_id": message_id, "error": type(exc).__name__},
            )
