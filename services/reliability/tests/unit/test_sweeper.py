"""The Reconciliation Sweeper's publishing side (the queries are tested in
dmer_common, against PostgreSQL)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import reliability.function_app as fa
import reliability.sweeper as sw
from dmer_common.db.status import PipelineStage
from dmer_common.db.sweeper import (
    DocumentSweep,
    OutboxBacklog,
    Republish,
    SweeperSettings,
    SweepQueue,
)

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def _item(queue, doc="doc-1", sweep=1, driver=None, blob="b.json"):
    return Republish(queue, doc, f"guid-{doc}", driver, blob, sweep)


def _run(monkeypatch, documents, signals, *, fail_queue=None):
    async def stalled(engine, *, settings, now):
        return documents

    async def stale(engine, *, settings, now):
        return signals

    async def backlog(engine, *, settings, now):
        return OutboxBacklog(0, 0, 0)

    monkeypatch.setattr(sw, "sweep_stalled_documents", stalled)
    monkeypatch.setattr(sw, "sweep_stale_evaluations", stale)
    monkeypatch.setattr(sw, "outbox_backlog", backlog)
    sent = []

    def send(queue, message):
        if queue is fail_queue:
            raise ConnectionError("down")
        sent.append((queue, message))

    summary = asyncio.run(
        sw.run_sweep(
            object(),
            send,
            settings=SweeperSettings(),
            clock=lambda: NOW,
            message_factory=lambda body, **kw: {"body": json.loads(body), **kw},
        )
    )
    return summary, sent


def test_republishes_carry_a_fresh_message_id_and_the_stage_s_blob(monkeypatch):
    summary, sent = _run(
        monkeypatch,
        DocumentSweep(republish=[_item(SweepQueue.RAW, sweep=2, blob="raw.pdf")]),
        [],
    )
    ((queue, message),) = sent
    assert queue is SweepQueue.RAW
    assert message["message_id"] == "guid-doc-1:sweep2"
    assert message["correlation_id"] == "doc-1"
    assert message["session_id"] is None
    assert message["body"] == {
        "schemaVersion": "1.0",
        "messageId": "guid-doc-1:sweep2",
        "documentId": "doc-1",
        "documentGuid": "guid-doc-1",
        "driverKey": None,
        "blobUrl": "raw.pdf",
        "attempt": 3,
        "enqueuedAt": "2026-10-19T12:00:00Z",
    }
    assert summary.republished == {"raw": 1}


def test_a_driver_signal_goes_to_the_driver_s_session(monkeypatch):
    _, sent = _run(
        monkeypatch,
        DocumentSweep(),
        [_item(SweepQueue.DRIVER_DECISION, driver="drv-1")],
    )
    ((queue, message),) = sent
    assert queue is SweepQueue.DRIVER_DECISION
    assert message["session_id"] == "drv-1"
    assert message["body"]["driverKey"] == "drv-1"


def test_a_failed_send_does_not_stop_the_others(monkeypatch):
    summary, sent = _run(
        monkeypatch,
        DocumentSweep(
            republish=[_item(SweepQueue.INGEST, "a"), _item(SweepQueue.RAW, "b")],
            manual_review=["c"],
        ),
        [],
        fail_queue=SweepQueue.INGEST,
    )
    assert [q for q, _ in sent] == [SweepQueue.RAW]
    assert summary.publish_failures == 1
    assert summary.manual_review == 1


def test_sweeper_settings_default_and_from_app_settings(monkeypatch):
    for name in (
        "STAGE_SLA_INGEST_MINUTES",
        "STAGE_SLA_EXTRACT_MINUTES",
        "STAGE_SLA_NORMALIZE_MINUTES",
        "STAGE_SLA_DRIVER_MINUTES",
        "SWEEPER_MAX_REPUBLISH",
        "SWEEPER_BATCH_SIZE",
        "OUTBOX_BACKLOG_SLA_MINUTES",
        "OUTBOX_ATTEMPT_ALERT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert fa.sweeper_settings() == SweeperSettings()
    monkeypatch.setenv("STAGE_SLA_EXTRACT_MINUTES", "45")
    monkeypatch.setenv("SWEEPER_MAX_REPUBLISH", "5")
    settings = fa.sweeper_settings()
    assert settings.stage_sla[PipelineStage.EXTRACT] == timedelta(minutes=45)
    assert settings.max_republish == 5


def test_each_sweep_queue_maps_to_its_pipeline_queue(monkeypatch):
    monkeypatch.setenv("DMER_EXTRACTED_QUEUE", "extracted-q")
    assert fa._queue_names() == {
        SweepQueue.INGEST: "dmer-ingest",
        SweepQueue.RAW: "dmer-raw",
        SweepQueue.EXTRACTED: "extracted-q",
        SweepQueue.DRIVER_DECISION: "driver-decision",
    }
