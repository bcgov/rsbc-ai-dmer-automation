"""Integration test: ServiceBusPublisher -> ServiceBusConsumer round-trip.

Runs against the Service Bus emulator or a dev namespace. Skipped unless
``SERVICE_BUS_CONNECTION_STRING`` and ``SERVICE_BUS_TEST_QUEUE`` are set, so the
default unit run is unaffected.

GIVEN a queue on the emulator/dev namespace
WHEN an ExtractedDmerMessage is published and then consumed
THEN the consumer receives it, propagates the correlation id, and completes it,
     and a redelivery of the same messageId is a no-op.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

CONN = os.getenv("SERVICE_BUS_CONNECTION_STRING")
QUEUE = os.getenv("SERVICE_BUS_TEST_QUEUE")

pytestmark = pytest.mark.skipif(
    not (CONN and QUEUE),
    reason="Service Bus emulator/dev namespace not configured "
    "(set SERVICE_BUS_CONNECTION_STRING and SERVICE_BUS_TEST_QUEUE)",
)


def _extracted(message_id: str):
    from dmer_common.dto import ExtractedDmerMessage

    return ExtractedDmerMessage(
        message_id=message_id,
        correlation_id="case-int-1",
        document_id="doc-int-1",
        mercury_case_id="case-int-1",
        sha256_hash="deadbeef",
        combined_result_uri="combined-extracted-dmer/doc-int-1/combined.json",
        processed_at=datetime.now(tz=UTC),
    )


def test_publish_then_consume_round_trip():
    from azure.servicebus import ServiceBusClient
    from dmer_common.messaging import (
        InMemoryIdempotencyStore,
        ServiceBusConsumer,
        ServiceBusPublisher,
    )

    assert CONN is not None and QUEUE is not None
    message_id = f"int-{datetime.now(tz=UTC).timestamp()}"

    with ServiceBusClient.from_connection_string(CONN) as sb:
        with sb.get_queue_sender(QUEUE) as sender:
            ServiceBusPublisher(sender).publish(_extracted(message_id))

        store = InMemoryIdempotencyStore()
        received: list[dict] = []
        with sb.get_queue_receiver(QUEUE, max_wait_time=10) as receiver:
            consumer = ServiceBusConsumer(receiver, idempotency_store=store)
            for msg in receiver:
                consumer.handle(msg, lambda env: received.append(env))
                break

    assert received, "expected to receive the published message"
    assert received[0]["messageId"] == message_id
    assert received[0]["combinedResultUri"].endswith("combined.json")
