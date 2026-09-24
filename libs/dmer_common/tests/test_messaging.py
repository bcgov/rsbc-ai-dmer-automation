"""Unit tests for the Service Bus consumer/publisher with a faked bus.

Behaviour specs (GIVEN/WHEN/THEN) for complete-on-success, dead-letter-on-failure,
idempotent no-op on duplicate messageId, correlation propagation, and envelope
publishing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from dmer_common.dto import ExtractedMessage
from dmer_common.messaging import (
    InMemoryIdempotencyStore,
    ServiceBusConsumer,
    ServiceBusPublisher,
)
from dmer_common.telemetry import get_correlation_id


class FakeMessage:
    def __init__(self, body: dict) -> None:
        self.body = json.dumps(body).encode("utf-8")


class FakeReceiver:
    def __init__(self) -> None:
        self.completed: list = []
        self.dead_lettered: list = []
        self.abandoned: list = []

    def complete_message(self, message) -> None:
        self.completed.append(message)

    def abandon_message(self, message) -> None:
        self.abandoned.append(message)

    def dead_letter_message(self, message, *, reason=None, error_description=None):
        self.dead_lettered.append((message, reason, error_description))


class FakeSender:
    def __init__(self) -> None:
        self.sent: list = []

    def send_messages(self, message) -> None:
        self.sent.append(message)


def _raw(message_id: str = "m-1", correlation_id: str = "case-1") -> FakeMessage:
    return FakeMessage(
        {
            "messageId": message_id,
            "correlationId": correlation_id,
            "schemaVersion": "1.0",
            "documentId": "doc-1",
        }
    )


def test_consumer_completes_on_success_and_propagates_correlation():
    # GIVEN a consumer and a message
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )
    seen = {}

    def handler(env: dict) -> None:
        seen["doc"] = env["documentId"]
        seen["corr"] = get_correlation_id()

    # WHEN the message is handled successfully
    ran = consumer.handle(_raw(), handler)
    # THEN the handler ran with the correlation id bound and the message completed
    assert ran is True
    assert seen == {"doc": "doc-1", "corr": "case-1"}
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []


def test_consumer_dead_letters_and_reraises_on_handler_error():
    # GIVEN a consumer whose handler raises
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )

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
    consumer = ServiceBusConsumer(
        receiver, idempotency_scope="test/queue", idempotency_store=store
    )
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
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )
    bad = FakeMessage({"correlationId": "case-1", "schemaVersion": "1.0"})
    # WHEN handled THEN it is dead-lettered and the handler never runs
    ran = consumer.handle(bad, lambda _e: pytest.fail("should not run"))
    assert ran is False
    assert len(receiver.dead_lettered) == 1


def test_publisher_serializes_envelope_and_sets_broker_ids():
    # GIVEN a publisher with a fake sender and a captured message factory
    sender = FakeSender()
    captured = {}

    def factory(body, *, message_id, correlation_id):
        captured["body"] = body
        captured["message_id"] = message_id
        captured["correlation_id"] = correlation_id
        return {"body": body}

    publisher = ServiceBusPublisher(sender, message_factory=factory)
    msg = ExtractedMessage(
        message_id="m-9",
        correlation_id="case-9",
        document_id="doc-1",
        document_guid="123e4567-e89b-12d3-a456-426614174000",
        driver_key="a91b77e4-0000-0000-0000-000000000000",
        blob_url="https://example/extracted-dmer/doc-1/combined.json",
        enqueued_at=datetime(2026, 8, 5, tzinfo=UTC),
    )
    # WHEN published
    publisher.publish(msg)
    # THEN the body is camelCase JSON and broker ids match the envelope
    assert len(sender.sent) == 1
    body = json.loads(captured["body"])
    assert body["messageId"] == "m-9"
    assert body["blobUrl"].endswith("combined.json")
    assert captured["message_id"] == "m-9"
    assert captured["correlation_id"] == "case-9"


class _ClassifiedError(Exception):
    """A handler error that supplies its own dead-letter reason + safe detail."""

    dead_letter_reason = "SOURCE_DOWNLOAD_FAILED"
    safe_detail = "error=ResourceNotFoundError; http_status=404"


def test_consumer_uses_handler_supplied_dead_letter_reason():
    # GIVEN a handler raising a classified error
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )

    def handler(_env: dict) -> None:
        raise _ClassifiedError("licence 01234567")

    with pytest.raises(_ClassifiedError):
        consumer.handle(_raw(), handler)

    # THEN its code and safe detail become the dead-letter reason/description
    _msg, reason, description = receiver.dead_lettered[0]
    assert reason == "SOURCE_DOWNLOAD_FAILED"
    assert description == "error=ResourceNotFoundError; http_status=404"


def test_consumer_falls_back_to_handler_error_for_plain_exceptions():
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )

    def handler(_env: dict) -> None:
        raise ValueError("anything")

    with pytest.raises(ValueError):
        consumer.handle(_raw(), handler)

    _msg, reason, description = receiver.dead_lettered[0]
    assert (reason, description) == ("HandlerError", "ValueError")


def test_consumer_never_logs_or_sends_the_exception_message(caplog):
    # GIVEN a handler error whose message carries extracted values
    receiver = FakeReceiver()
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope="test/queue",
        idempotency_store=InMemoryIdempotencyStore(),
    )

    def handler(_env: dict) -> None:
        raise RuntimeError("licence 01234567; dx: epilepsy")

    with caplog.at_level("DEBUG"), pytest.raises(RuntimeError):
        consumer.handle(_raw(), handler)

    # THEN the text is in neither the log nor the dead-letter fields
    assert "01234567" not in caplog.text
    assert "epilepsy" not in caplog.text
    _msg, reason, description = receiver.dead_lettered[0]
    assert "01234567" not in f"{reason} {description}"


def test_idempotency_is_scoped_per_consumer():
    # GIVEN one shared store and two consumers of different queues
    store = InMemoryIdempotencyStore()
    first = ServiceBusConsumer(
        FakeReceiver(),
        idempotency_scope="di-processor/dmer-raw",
        idempotency_store=store,
    )
    second_receiver = FakeReceiver()
    second = ServiceBusConsumer(
        second_receiver,
        idempotency_scope="document-orchestrator/dmer-extracted",
        idempotency_store=store,
    )
    calls = []

    # WHEN both see a message with the same messageId
    first.handle(_raw("same-id"), lambda env: calls.append("first"))
    ran = second.handle(_raw("same-id"), lambda env: calls.append("second"))

    # THEN the second consumer still processes it (not suppressed as a duplicate)
    assert ran is True
    assert calls == ["first", "second"]


def test_consumer_requires_an_idempotency_scope():
    with pytest.raises(ValueError, match="idempotency_scope"):
        ServiceBusConsumer(
            FakeReceiver(),
            idempotency_scope="",
            idempotency_store=InMemoryIdempotencyStore(),
        )


# --- durable claim semantics (claim -> handle -> complete / release) ---------


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _consumer(store, receiver=None, scope="di-processor/dmer-raw"):
    return ServiceBusConsumer(
        receiver or FakeReceiver(), idempotency_scope=scope, idempotency_store=store
    )


def test_consumer_requires_an_idempotency_store():
    # GIVEN no store THEN construction fails: there is no in-memory default
    with pytest.raises(TypeError):
        ServiceBusConsumer(FakeReceiver(), idempotency_scope="x")


def test_duplicate_suppressed_across_consumer_instances():
    # GIVEN two separate consumer instances (e.g. two replicas) on one store
    store = InMemoryIdempotencyStore()
    calls = []
    first_rx, second_rx = FakeReceiver(), FakeReceiver()
    _consumer(store, first_rx).handle(_raw("m-1"), lambda e: calls.append(1))

    # WHEN the same message is delivered to the second after completion
    ran = _consumer(store, second_rx).handle(_raw("m-1"), lambda e: calls.append(2))

    # THEN the handler ran once; the duplicate is completed without running it
    assert calls == [1]
    assert ran is False
    assert len(second_rx.completed) == 1


def test_message_in_progress_elsewhere_is_abandoned_not_completed():
    # GIVEN another worker holds a live claim on the message
    store = InMemoryIdempotencyStore()
    store.claim("di-processor/dmer-raw", "m-1")
    receiver = FakeReceiver()

    ran = _consumer(store, receiver).handle(
        _raw("m-1"), lambda e: pytest.fail("must not run")
    )

    # THEN it is abandoned for redelivery — never completed (which could lose it)
    assert ran is False
    assert len(receiver.abandoned) == 1
    assert receiver.completed == []
    assert receiver.dead_lettered == []


def test_handler_failure_releases_claim_so_message_stays_retryable():
    store = InMemoryIdempotencyStore()
    receiver = FakeReceiver()

    def boom(_env):
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        _consumer(store, receiver).handle(_raw("m-1"), boom)

    # THEN it is dead-lettered, and the claim was released (not completed), so a
    # redrive of the same message runs the handler again
    assert len(receiver.dead_lettered) == 1
    calls = []
    assert _consumer(store).handle(_raw("m-1"), lambda e: calls.append(1)) is True
    assert calls == [1]


def test_crashed_worker_claim_is_taken_over_after_lease_expiry():
    # GIVEN a worker claimed the message and crashed (never completed/released)
    clock = _Clock()
    store = InMemoryIdempotencyStore(lease_seconds=300, clock=clock)
    store.claim("di-processor/dmer-raw", "m-1")

    # WHEN redelivered before the lease expires THEN it is left alone
    rx = FakeReceiver()
    assert _consumer(store, rx).handle(_raw("m-1"), lambda e: None) is False
    assert len(rx.abandoned) == 1

    # WHEN redelivered after the lease expires THEN another worker takes over
    clock.now = 301
    calls = []
    assert _consumer(store).handle(_raw("m-1"), lambda e: calls.append(1)) is True
    assert calls == [1]


def test_completion_record_failure_still_completes_the_message():
    # GIVEN the store fails to record completion after a successful handler
    class FlakyStore(InMemoryIdempotencyStore):
        def complete(self, scope, message_id, token):
            raise ConnectionError("db down")

    receiver = FakeReceiver()
    ran = _consumer(FlakyStore(), receiver).handle(_raw("m-1"), lambda e: None)

    # THEN the message is still completed (the handler's work is done)
    assert ran is True
    assert len(receiver.completed) == 1


def test_claim_failure_leaves_message_unsettled():
    # GIVEN the store is unavailable when claiming
    class DownStore(InMemoryIdempotencyStore):
        def claim(self, scope, message_id):
            raise ConnectionError("db down")

    receiver = FakeReceiver()
    with pytest.raises(ConnectionError):
        _consumer(DownStore(), receiver).handle(
            _raw("m-1"), lambda e: pytest.fail("must not run")
        )

    # THEN nothing is settled: the lock expires and Service Bus redelivers it
    assert receiver.completed == receiver.dead_lettered == receiver.abandoned == []


def test_stale_token_cannot_complete_or_release_a_taken_over_claim():
    clock = _Clock()
    store = InMemoryIdempotencyStore(lease_seconds=10, clock=clock)
    old = store.claim("s", "m-1").token
    clock.now = 11
    new = store.claim("s", "m-1").token  # takeover
    assert old != new

    # THEN the original worker's token no longer controls the record
    assert store.complete("s", "m-1", old) is False
    store.release("s", "m-1", old)
    assert store.claim("s", "m-1").outcome.value == "IN_PROGRESS"
    assert store.complete("s", "m-1", new) is True
    assert store.claim("s", "m-1").outcome.value == "DUPLICATE"
