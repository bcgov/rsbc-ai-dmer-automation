"""Envelope-aware Service Bus publisher.

Publishes DTOs that extend :class:`dmer_common.dto.Envelope`, serializing them to
the camelCase wire format and setting Service Bus ``message_id`` from the
envelope, plus the SDK's own broker-level ``correlation_id`` property from
``document_id`` (our own tracing key -- see ``docs/development/data-model.md``),
so downstream tracing tools that read that native AMQP field still see it
(Requirement 7.2).

The underlying Azure ``ServiceBusSender`` and the message factory are injected so
the serialization/wiring logic is unit-testable with a fake sender.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from ..dto import Envelope
from ..telemetry import get_logger

_log = get_logger(__name__)


class _Sender(Protocol):
    """Subset of ``azure.servicebus.ServiceBusSender`` this publisher uses."""

    def send_messages(self, message: Any) -> None: ...


def _default_message_factory(
    body: str, *, message_id: str, document_id: str, session_id: str | None = None
) -> Any:
    """Build an ``azure.servicebus.ServiceBusMessage`` (imported lazily).

    ``document_id`` is passed through as the SDK's own ``correlation_id``
    keyword -- a broker-level AMQP field, unrelated to our envelope's own
    field naming, which no longer has a ``correlation_id`` of its own.
    ``session_id`` is set only for a session-enabled queue.
    """
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(
        body, message_id=message_id, correlation_id=document_id, session_id=session_id
    )


class ServiceBusPublisher:
    """Publishes envelope DTOs to a single queue/topic."""

    def __init__(
        self,
        sender: _Sender,
        *,
        message_factory: Callable[..., Any] = _default_message_factory,
    ) -> None:
        self._sender = sender
        self._message_factory = message_factory

    def publish(self, envelope: Envelope, *, session_id: str | None = None) -> None:
        """Serialize and send an envelope DTO with broker ids set.

        ``session_id`` is required by a session-enabled queue (``driver-decision``:
        ``SessionId = driver_key``) and must be omitted for the others.
        """
        body = envelope.model_dump_json(by_alias=True)
        ids = {"message_id": envelope.message_id, "document_id": envelope.document_id}
        if session_id is not None:
            ids["session_id"] = session_id
        message = self._message_factory(body, **ids)
        self._sender.send_messages(message)
        _log.info(
            "published message",
            extra={
                "message_id": envelope.message_id,
                "document_id": envelope.document_id,
                "schema_version": envelope.schema_version,
            },
        )
