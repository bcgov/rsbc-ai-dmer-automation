"""Envelope-aware Service Bus publisher.

Publishes DTOs that extend :class:`dmer_common.dto.Envelope`, serializing them to
the camelCase wire format and setting Service Bus ``message_id`` and
``correlation_id`` from the envelope so broker-level tracing/dedupe aligns with
the payload (Requirement 7.2).

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


def _default_message_factory(body: str, *, message_id: str, correlation_id: str) -> Any:
    """Build an ``azure.servicebus.ServiceBusMessage`` (imported lazily)."""
    from azure.servicebus import ServiceBusMessage

    return ServiceBusMessage(body, message_id=message_id, correlation_id=correlation_id)


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

    def publish(self, envelope: Envelope) -> None:
        """Serialize and send an envelope DTO with broker id/correlation set."""
        body = envelope.model_dump_json(by_alias=True)
        message = self._message_factory(
            body,
            message_id=envelope.message_id,
            correlation_id=envelope.correlation_id,
        )
        self._sender.send_messages(message)
        _log.info(
            "published message",
            extra={
                "message_id": envelope.message_id,
                "correlation_id": envelope.correlation_id,
                "schema_version": envelope.schema_version,
            },
        )
