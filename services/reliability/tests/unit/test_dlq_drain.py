"""The DLQ Drain's Service Bus side: what is re-sent, completed or left in
the DLQ (the classification and database writes are tested in dmer_common)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import reliability.dlq_drain as dd
import reliability.function_app as fa
from dmer_common.db.dlq_drain import DrainAction, DrainResult
from dmer_common.db.status import PipelineStage, PipelineStatus

T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)
RAW = dd.DrainSource("dmer-raw", PipelineStage.EXTRACT, PipelineStatus.DOWNLOADED)
SETTINGS = dd.DrainSettings(batch_size=10, redrive_backoff=timedelta(minutes=5))


def _message(body, *, reason="DB_WRITE_FAILED", message_id="m-1"):
    return SimpleNamespace(
        body=json.dumps(body).encode(),
        message_id=message_id,
        correlation_id=body.get("documentId"),
        dead_letter_reason=reason,
        dead_letter_error_description="error=OperationalError",
    )


class FakeReceiver:
    def __init__(self, messages):
        self.messages = messages
        self.completed, self.abandoned = [], []

    def receive_messages(self, max_message_count, max_wait_time):
        return self.messages[:max_message_count]

    def complete_message(self, message):
        self.completed.append(message.message_id)

    def abandon_message(self, message):
        self.abandoned.append(message.message_id)


class FakeSender:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_messages(self, message):
        if self.fail:
            raise ConnectionError("down")
        self.sent.append(message)


def _factory(body, **kwargs):
    return {"body": json.loads(body), **kwargs}


def _run(monkeypatch, results, messages, *, source=RAW, sender=None):
    seen = []

    async def fake_drain(engine, dead_letter, *, max_redrives, now):
        seen.append(dead_letter)
        result = results[dead_letter.message_id]
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(dd, "drain_dead_letter", fake_drain)
    receiver, sender = FakeReceiver(messages), sender or FakeSender()
    summary = asyncio.run(
        dd.drain_source(
            object(),
            source,
            receiver,
            sender,
            settings=SETTINGS,
            clock=lambda: T0,
            message_factory=_factory,
        )
    )
    return summary, receiver, sender, seen


BODY = {"messageId": "orig", "documentId": "doc-1", "attempt": 1, "blobUrl": "u"}


def test_a_redrive_is_resent_with_a_new_id_and_attempt_then_completed(monkeypatch):
    summary, receiver, sender, seen = _run(
        monkeypatch,
        {"m-1": DrainResult(DrainAction.REDRIVEN, None, redrive_attempt=2)},
        [_message(BODY)],
    )
    (sent,) = sender.sent
    assert sent["body"] == {**BODY, "messageId": "orig:redrive2", "attempt": 2}
    assert sent["message_id"] == "orig:redrive2"
    assert sent["correlation_id"] == "doc-1"
    assert sent["session_id"] is None
    # backoff doubles per redrive: 5 min, then 10
    assert sent["scheduled_enqueue_time_utc"] == T0 + timedelta(minutes=10)
    assert receiver.completed == ["m-1"]
    assert summary.actions == {"REDRIVEN": 1}
    (dead_letter,) = seen
    assert (dead_letter.document_id, dead_letter.stage, dead_letter.reason) == (
        "doc-1",
        "EXTRACT",
        "DB_WRITE_FAILED",
    )
    assert dead_letter.entry_status is PipelineStatus.DOWNLOADED


@pytest.mark.parametrize(
    "action",
    [DrainAction.FALLBACK_QUEUED, DrainAction.MANUAL_REVIEW, DrainAction.STALE],
)
def test_anything_else_is_completed_without_a_resend(monkeypatch, action):
    summary, receiver, sender, _ = _run(
        monkeypatch, {"m-1": DrainResult(action, None)}, [_message(BODY)]
    )
    assert sender.sent == []
    assert receiver.completed == ["m-1"]
    assert summary.actions == {action.value: 1}


def test_a_failed_drain_leaves_the_dead_letter_for_the_next_run(monkeypatch):
    summary, receiver, _, _ = _run(
        monkeypatch,
        {"m-1": RuntimeError("db down"), "m-2": DrainResult(DrainAction.STALE, None)},
        [_message(BODY), _message(BODY, message_id="m-2")],
    )
    assert receiver.abandoned == ["m-1"]
    assert receiver.completed == ["m-2"]  # one bad message doesn't stop the rest
    assert summary.left_in_dlq == 1


def test_a_failed_resend_is_not_completed(monkeypatch):
    _, receiver, _, _ = _run(
        monkeypatch,
        {"m-1": DrainResult(DrainAction.REDRIVEN, None, redrive_attempt=1)},
        [_message(BODY)],
        sender=FakeSender(fail=True),
    )
    assert receiver.completed == [] and receiver.abandoned == ["m-1"]


def test_driver_decision_redrive_keeps_the_session_and_the_attempt(monkeypatch):
    source = dd.pipeline_sources(
        ingest="i", raw="r", extracted="e", driver_decision="driver-decision"
    )[3]
    body = {**BODY, "driverKey": "drv-1", "attempt": 3}
    _, _, sender, seen = _run(
        monkeypatch,
        {"m-1": DrainResult(DrainAction.REDRIVEN, None, redrive_attempt=1)},
        [_message(body, reason="MaxDeliveryCountExceeded")],
        source=source,
    )
    (sent,) = sender.sent
    assert sent["session_id"] == "drv-1"
    assert sent["body"]["attempt"] == 3  # counts Mercury re-checks, not redrives
    assert seen[0].routes_document is False and seen[0].entry_status is None


def test_a_body_that_is_not_json_still_drains_by_correlation_id(monkeypatch):
    message = SimpleNamespace(
        body=b"not json",
        message_id="m-1",
        correlation_id="doc-9",
        dead_letter_reason="UNEXPECTED",
        dead_letter_error_description=None,
    )
    _, receiver, _, seen = _run(
        monkeypatch, {"m-1": DrainResult(DrainAction.MANUAL_REVIEW, None)}, [message]
    )
    assert seen[0].document_id == "doc-9"
    assert receiver.completed == ["m-1"]


def test_drain_settings_default_and_from_app_settings(monkeypatch):
    for name in (
        "DLQ_DRAIN_BATCH_SIZE",
        "DLQ_REDRIVE_MAX_ATTEMPTS",
        "DLQ_REDRIVE_BACKOFF_SECONDS",
        "FALLBACK_ENABLED_CATEGORIES",
    ):
        monkeypatch.delenv(name, raising=False)
    assert fa.drain_settings() == dd.DrainSettings()
    monkeypatch.setenv("DLQ_REDRIVE_MAX_ATTEMPTS", "5")
    monkeypatch.setenv("DLQ_REDRIVE_BACKOFF_SECONDS", "60")
    assert fa.drain_settings().max_redrives == 5
    assert fa.drain_settings().redrive_backoff == timedelta(seconds=60)


@pytest.mark.parametrize("value", ["TRANSIENT", "PERMANENT_BUSINESS,UNKNOWN", ""])
def test_no_category_but_permanent_business_may_produce_a_fallback(monkeypatch, value):
    monkeypatch.setenv("FALLBACK_ENABLED_CATEGORIES", value)
    with pytest.raises(ValueError):
        fa.drain_settings()


def test_the_four_pipeline_queues_are_drained_from_app_settings(monkeypatch):
    monkeypatch.setenv("DMER_RAW_QUEUE", "raw-q")
    sources = {s.queue: s for s in fa.drain_sources()}
    assert set(sources) == {"dmer-ingest", "raw-q", "dmer-extracted", "driver-decision"}
    assert sources["raw-q"].entry_status is PipelineStatus.DOWNLOADED
    assert sources["dmer-ingest"].entry_status is PipelineStatus.RECEIVED
    assert sources["dmer-extracted"].entry_status is PipelineStatus.EXTRACTED
