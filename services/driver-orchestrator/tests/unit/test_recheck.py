"""The self-scheduled re-check of a driver Mercury could not verify."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import driver_orchestrator.function_app as fa
import pytest
from dmer_common.dto import (
    DRIVER_DECISION_EVENT,
    DriverDecisionMessage,
    event_message_id,
)
from driver_orchestrator.decision import WaitReason
from driver_orchestrator.evaluate import Evaluation, Result
from driver_orchestrator.recheck import RECHECK_DELAYS, recheck_delay, recheck_message

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"


def _message(attempt: int = 1) -> DriverDecisionMessage:
    return DriverDecisionMessage(
        message_id="m-1",
        document_id="123e4567-e89b-12d3-a456-426655440000",
        document_guid="258a9a03-ee50-4363-8519-ae546fab4add",
        driver_key=DRIVER_KEY,
        blob_url="https://blob/normalized-dmer/doc.json",
        attempt=attempt,
        enqueued_at=NOW,
    )


UNVERIFIED = Evaluation(Result.WAITING, "eval-1", WaitReason.MERCURY_UNVERIFIED)


# --- backoff ---------------------------------------------------------------


def test_delays_grow_then_stop():
    assert [recheck_delay(a) for a in range(1, 7)] == [
        timedelta(minutes=15),
        timedelta(minutes=30),
        timedelta(hours=1),
        timedelta(hours=2),
        timedelta(hours=4),
        None,  # re-checks used up after attempt 5's re-check
    ]
    assert sum(RECHECK_DELAYS, timedelta()) == timedelta(hours=7, minutes=45)


@pytest.mark.parametrize("attempt", [0, -1])
def test_an_out_of_range_attempt_schedules_nothing(attempt):
    assert recheck_delay(attempt) is None


def test_recheck_message_keeps_the_driver_and_counts_the_attempt():
    original = _message(attempt=2)
    recheck = recheck_message(original, now=NOW)
    assert recheck.driver_key == DRIVER_KEY
    assert (recheck.document_id, recheck.blob_url) == (
        original.document_id,
        original.blob_url,
    )
    assert recheck.attempt == 3
    assert recheck.enqueued_at == NOW
    # its own id, never the original signal's or Signal Driver's
    assert recheck.message_id != original.message_id
    assert recheck.message_id != event_message_id(
        DRIVER_DECISION_EVENT, original.document_id
    )
    assert recheck_message(original, now=NOW).message_id == recheck.message_id


# --- the trigger schedules it -----------------------------------------------


def _schedule(message, evaluation, publish):
    return asyncio.run(
        fa.schedule_recheck_if_unverified(
            message, evaluation, publish=publish, clock=lambda: NOW
        )
    )


def test_mercury_unverified_schedules_the_next_recheck():
    sent = []
    due = _schedule(_message(attempt=1), UNVERIFIED, lambda m, t: sent.append((m, t)))
    assert due == NOW + timedelta(minutes=15)
    ((message, scheduled_for),) = sent
    assert scheduled_for == due
    assert (message.attempt, message.driver_key) == (2, DRIVER_KEY)


def test_a_later_recheck_waits_longer():
    sent = []
    due = _schedule(_message(attempt=4), UNVERIFIED, lambda m, t: sent.append(t))
    assert due == NOW + timedelta(hours=2)


def test_rechecks_stop_when_used_up():
    sent = []
    assert (
        _schedule(_message(attempt=6), UNVERIFIED, lambda m, t: sent.append(m)) is None
    )
    assert sent == []


@pytest.mark.parametrize(
    "evaluation",
    [
        Evaluation(Result.DECIDED, "eval-1"),
        Evaluation(Result.NO_OPEN_EVALUATION),
        Evaluation(Result.WAITING, "eval-1", WaitReason.WAITING_FOR_DOCUMENTS),
        Evaluation(Result.WAITING, "eval-1", WaitReason.DOCUMENTS_STILL_PROCESSING),
    ],
)
def test_only_mercury_unverified_schedules(evaluation):
    # Other waits end by the DMERs' own signals.
    sent = []
    assert _schedule(_message(), evaluation, lambda m, t: sent.append(m)) is None
    assert sent == []


def test_a_failed_schedule_is_logged_not_raised():
    def fail(message, scheduled_for):
        raise ConnectionError("Service Bus unavailable")

    assert _schedule(_message(), UNVERIFIED, fail) is None


def test_handle_schedules_after_an_unverified_evaluation(monkeypatch):
    import json

    sent = []

    async def fake_evaluate(engine, driver_key, **kwargs):
        return UNVERIFIED

    class _Engine:
        async def dispose(self):
            pass

    async def engine_factory():
        return _Engine()

    class _Mercury:
        class driver_settings:
            counted_document_types = frozenset({"dmer"})
            uncounted_document_statuses = frozenset({"rejected"})

        def get_driver_by_licence(self, licence):  # pragma: no cover
            return []

    monkeypatch.setattr(fa, "evaluate_driver", fake_evaluate)
    body = json.dumps(
        {
            "messageId": "m-1",
            "schemaVersion": "1.0",
            "documentId": "123e4567-e89b-12d3-a456-426655440000",
            "documentGuid": "258a9a03-ee50-4363-8519-ae546fab4add",
            "driverKey": DRIVER_KEY,
            "blobUrl": "https://blob/normalized-dmer/doc.json",
            "attempt": 1,
            "enqueuedAt": "2026-10-14T12:00:00Z",
        }
    ).encode()
    result = asyncio.run(
        fa.handle_driver_signal(
            body,
            engine_factory=engine_factory,
            mercury=_Mercury(),
            publish=lambda m, t: sent.append(m),
        )
    )
    assert result is UNVERIFIED
    assert [m.attempt for m in sent] == [2]


@pytest.mark.parametrize("attempt,final", [(1, False), (5, False), (6, True)])
def test_only_the_last_recheck_is_final(monkeypatch, attempt, final):
    import json

    seen = {}

    async def fake_evaluate(engine, driver_key, **kwargs):
        seen["final_attempt"] = kwargs["final_attempt"]
        return Evaluation(Result.DECIDED, "eval-1")

    class _Engine:
        async def dispose(self):
            pass

    async def engine_factory():
        return _Engine()

    class _Mercury:
        class driver_settings:
            counted_document_types = frozenset({"dmer"})
            uncounted_document_statuses = frozenset({"rejected"})

        def get_driver_by_licence(self, licence):  # pragma: no cover
            return []

    monkeypatch.setattr(fa, "evaluate_driver", fake_evaluate)
    body = json.dumps(
        {
            "messageId": "m-1",
            "schemaVersion": "1.0",
            "documentId": "123e4567-e89b-12d3-a456-426655440000",
            "documentGuid": "258a9a03-ee50-4363-8519-ae546fab4add",
            "driverKey": DRIVER_KEY,
            "blobUrl": "https://blob/normalized-dmer/doc.json",
            "attempt": attempt,
            "enqueuedAt": "2026-10-14T12:00:00Z",
        }
    ).encode()
    asyncio.run(
        fa.handle_driver_signal(
            body,
            engine_factory=engine_factory,
            mercury=_Mercury(),
            publish=lambda m, t: None,
        )
    )
    assert seen["final_attempt"] is final


def test_no_recheck_after_manual_review():
    sent = []
    manual = Evaluation(Result.MANUAL_REVIEW, "eval-1", WaitReason.MERCURY_UNVERIFIED)
    assert _schedule(_message(attempt=6), manual, lambda m, t: sent.append(m)) is None
    assert sent == []
