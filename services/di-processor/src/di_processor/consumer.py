"""Thin ``dmer-raw`` consumer adapter.

Binds the shared :class:`dmer_common.messaging.ServiceBusConsumer` (which handles
envelope parsing, correlation propagation, idempotency, and settlement) to the
application :meth:`Pipeline.run`. This adapter is deliberately thin: it only
bridges the consumer's synchronous, dict-based handler contract to the async,
typed pipeline, and owns no business logic.

Settlement is owned by ``ServiceBusConsumer``: handler success -> complete;
handler raises -> dead-letter (native DLQ). The pipeline raises on unrecoverable
failure (after routing the document to ``MANUAL_REVIEW``), which propagates here
and lets the message dead-letter.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from dmer_common.dto import RawMessage
from dmer_common.messaging import ServiceBusConsumer

from .pipeline import Pipeline


def make_handler(pipeline: Pipeline) -> Callable[[dict[str, Any]], None]:
    """Return a synchronous handler that runs ``pipeline.run`` for one envelope.

    The shared consumer invokes the handler with the parsed envelope dict; we
    validate it into a :class:`RawMessage` and drive the async pipeline to
    completion on the current thread. Any exception propagates so the consumer
    dead-letters the message.
    """

    def handle(envelope: dict[str, Any]) -> None:
        message = RawMessage.model_validate(envelope)
        asyncio.run(pipeline.run(message))

    return handle


def idempotency_scope(queue: str) -> str:
    """This consumer's name in the shared idempotency store."""
    return f"di-processor/{queue}"


def make_consumer(
    receiver: Any, pipeline: Pipeline, *, queue: str = "dmer-raw", **kwargs: Any
) -> tuple[ServiceBusConsumer, Callable[[dict[str, Any]], None]]:
    """Build the shared consumer plus the pipeline-bound handler.

    ``receiver`` is an Azure ``ServiceBusReceiver`` for ``queue``; ``kwargs``
    are forwarded to :class:`ServiceBusConsumer` (e.g. a durable
    ``idempotency_store``). Returns the consumer and the handler to pass to
    :meth:`ServiceBusConsumer.handle` per message.
    """
    consumer = ServiceBusConsumer(
        receiver, idempotency_scope=idempotency_scope(queue), **kwargs
    )
    return consumer, make_handler(pipeline)
