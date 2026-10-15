"""The DriverDecision trigger's own logic: envelope parsing and wiring to
evaluate_driver (database, Mercury and the evaluation itself faked)."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import driver_orchestrator.function_app as fa
import pytest
from driver_orchestrator.evaluate import Evaluation, Result

DOCUMENT_ID = "123e4567-e89b-12d3-a456-426655440000"
DOCUMENT_GUID = "258a9a03-ee50-4363-8519-ae546fab4add"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
BLOB_URL = "https://rsbcstorage.blob.core.windows.net/normalized-dmer/doc.json"


def _envelope(**overrides) -> bytes:
    body = {
        "messageId": "m-1",
        "schemaVersion": "1.0",
        "documentId": DOCUMENT_ID,
        "documentGuid": DOCUMENT_GUID,
        "driverKey": DRIVER_KEY,
        "blobUrl": BLOB_URL,
        "attempt": 1,
        "enqueuedAt": "2026-10-09T10:00:00Z",
    }
    body.update(overrides)
    return json.dumps({k: v for k, v in body.items() if v is not None}).encode()


class _Engine:
    disposed = False

    async def dispose(self):
        self.disposed = True


class _Mercury:
    driver_settings = SimpleNamespace(
        counted_document_types=frozenset({"dmer"}),
        uncounted_document_statuses=frozenset({"rejected"}),
    )

    def get_driver_by_licence(self, licence):  # pragma: no cover - not reached
        return []


def test_parse_signal_reads_the_envelope():
    message = fa.parse_signal(_envelope())
    assert (message.driver_key, message.document_id) == (DRIVER_KEY, DOCUMENT_ID)


@pytest.mark.parametrize("driver_key", [None, "not-a-uuid"])
def test_parse_signal_rejects_a_missing_or_invalid_driver_key(driver_key):
    with pytest.raises(ValueError):
        fa.parse_signal(_envelope(driverKey=driver_key))


def test_parse_signal_rejects_malformed_json():
    with pytest.raises(ValueError):
        fa.parse_signal(b"{not json")


def test_handle_runs_one_evaluation_for_the_signalled_driver(monkeypatch):
    calls = []
    engine = _Engine()

    async def fake_evaluate(eng, driver_key, **kwargs):
        calls.append((eng, driver_key, kwargs))
        return Evaluation(Result.DECIDED, "eval-1", decided_count=2)

    monkeypatch.setattr(fa, "evaluate_driver", fake_evaluate)

    async def engine_factory():
        return engine

    result = asyncio.run(
        fa.handle_driver_signal(
            _envelope(), engine_factory=engine_factory, mercury=_Mercury()
        )
    )
    assert result.result is Result.DECIDED
    ((eng, driver_key, kwargs),) = calls
    assert (eng, driver_key) == (engine, DRIVER_KEY)
    assert kwargs["counted_types"] == frozenset({"dmer"})
    assert kwargs["uncounted_statuses"] == frozenset({"rejected"})
    assert engine.disposed


def test_handle_disposes_the_engine_and_re_raises_on_failure(monkeypatch):
    engine = _Engine()

    async def failing_evaluate(*args, **kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(fa, "evaluate_driver", failing_evaluate)

    async def engine_factory():
        return engine

    with pytest.raises(RuntimeError):
        asyncio.run(
            fa.handle_driver_signal(
                _envelope(), engine_factory=engine_factory, mercury=_Mercury()
            )
        )
    assert engine.disposed  # so Service Bus redelivers with nothing leaked


def test_malformed_message_never_touches_the_database():
    opened = []

    async def engine_factory():  # pragma: no cover - must not be reached
        opened.append(True)

    with pytest.raises(ValueError):
        asyncio.run(
            fa.handle_driver_signal(
                _envelope(driverKey=None), engine_factory=engine_factory
            )
        )
    assert not opened


def test_trigger_is_registered_with_sessions_enabled():
    functions = {f.get_function_name(): f for f in fa.app.get_functions()}
    trigger = functions["DriverDecision"].get_trigger()
    assert trigger.queue_name == "%DRIVER_DECISION_QUEUE%"
    assert trigger.connection == "ServiceBusConnection"
    assert trigger.is_sessions_enabled is True
