"""Unit tests for the Service Bus consumer/publisher with a faked bus.

Behaviour specs (GIVEN/WHEN/THEN) for complete-on-success, dead-letter-on-failure,
idempotent no-op on duplicate messageId, document-id propagation, and envelope
publishing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from dmer_common.dto import ExtractedDmerMessage
from dmer_common.messaging import (
    InMemoryIdempotencyStore,
    ServiceBusConsumer,
    ServiceBusPublisher,
)
from dmer_common.telemetry import get_document_id


class FakeMessage:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode("utf-8")


class FakeReceiver:
    def __init__(self) -> None:
        self.completed: list = []
        self.dead_lettered: list = []

    def complete_message(self, message) -> None:
        self.completed.append(message)

    def dead_letter_message(self, message, *, reason=None, error_description=None):
        self.dead_lettered.append((message, reason, error_description))


class FakeSender:
    def __init__(self) -> None:
        self.sent: list = []

    def send_messages(self, message) -> None:
        self.sent.append(message)


def _raw(message_id: str = "m-1", document_id: str = "doc-1") -> FakeMessage:
    return FakeMessage(
        {
            "messageId": message_id,
            "schemaVersion": "1.0",
            "documentId": document_id,
        }
    )


def test_consumer_completes_on_success_and_propagates_document_id():
    # GIVEN a consumer and a message
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(receiver)
    seen = {}

    def handler(env: dict) -> None:
        seen["doc"] = env["documentId"]
        seen["bound_doc"] = get_document_id()

    # WHEN the message is handled successfully
    ran = consumer.handle(_raw(), handler)
    # THEN the handler ran with the document id bound and the message completed
    assert ran is True
    assert seen == {"doc": "doc-1", "bound_doc": "doc-1"}
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []


def test_consumer_dead_letters_and_reraises_on_handler_error():
    # GIVEN a consumer whose handler raises
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(receiver)

    def handler(_env: dict) -> None:
        raise RuntimeError("processing failed")

    # WHEN the message is handled THEN it is dead-lettered and the error re-raised
    with pytest.raises(RuntimeError, match="processing failed"):
        consumer.handle(_raw(), handler)
    assert len(receiver.dead_lettered) == 1
    assert receiver.completed == []


def test_consumer_is_idempotent_on_duplicate_message_id():
    # GIVEN a shared idempotency store and two messages with the same messageId
    receiver = FakeReceiver()
    store = InMemoryIdempotencyStore()
    consumer = ServiceBusConsumer(receiver, idempotency_store=store)
    count = {"n": 0}

    def handler(_env: dict) -> None:
        count["n"] += 1

    # WHEN the same messageId is handled twice
    first = consumer.handle(_raw("dup"), handler)
    second = consumer.handle(_raw("dup"), handler)
    # THEN the handler runs once; the duplicate is a no-op that completes
    assert first is True
    assert second is False
    assert count["n"] == 1
    assert len(receiver.completed) == 2


def test_consumer_dead_letters_message_without_message_id():
    # GIVEN a message missing messageId
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(receiver)
    bad = FakeMessage({"documentId": "doc-1", "schemaVersion": "1.0"})
    # WHEN handled THEN it is dead-lettered and the handler never runs
    ran = consumer.handle(bad, lambda _e: pytest.fail("should not run"))
    assert ran is False
    assert len(receiver.dead_lettered) == 1


def test_publisher_serializes_envelope_and_sets_broker_ids():
    # GIVEN a publisher with a fake sender and a captured message factory
    sender = FakeSender()
    captured = {}

    def factory(body, *, message_id, document_id):
        captured["body"] = body
        captured["message_id"] = message_id
        captured["document_id"] = document_id
        return {"body": body}

    publisher = ServiceBusPublisher(sender, message_factory=factory)
    msg = ExtractedDmerMessage(
        message_id="m-9",
        document_id="doc-1",
        mercury_case_id="case-9",
        sha256_hash="abc",
        combined_result_uri="combined-extracted-dmer/doc-1/combined.json",
        processed_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    # WHEN published
    publisher.publish(msg)
    # THEN the body is camelCase JSON and broker ids match the envelope
    assert len(sender.sent) == 1
    body = json.loads(captured["body"])
    assert body["messageId"] == "m-9"
    assert body["combinedResultUri"].endswith("combined.json")
    assert captured["message_id"] == "m-9"
    assert captured["document_id"] == "doc-1"
