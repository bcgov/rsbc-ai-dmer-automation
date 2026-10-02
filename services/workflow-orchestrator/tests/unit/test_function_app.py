"""StartDocumentOrchestration's own logic: envelope parsing and the
already-in-flight/already-done idempotency check (Durable client mocked)."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.durable_functions.models.OrchestrationRuntimeStatus import (
    OrchestrationRuntimeStatus,
)

import workflow_orchestrator.function_app as fa

DOCUMENT_ID = "123e4567-e89b-12d3-a456-426655440000"
DOCUMENT_GUID = "258a9a03-ee50-4363-8519-ae546fab4add"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
BLOB_URL = "https://rsbcstorage.blob.core.windows.net/extracted-dmer/doc.json"


def _envelope(**overrides) -> bytes:
    body = {
        "schemaVersion": "1.0",
        "documentId": DOCUMENT_ID,
        "documentGuid": DOCUMENT_GUID,
        "driverKey": DRIVER_KEY,
        "blobUrl": BLOB_URL,
        "attempt": 1,
        "enqueuedAt": "2026-09-22T22:04:14.591975Z",
    }
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


class _FakeMessage:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def get_body(self) -> bytes:
        return self._body


def _fake_client(existing_status=None):
    return SimpleNamespace(
        get_status=AsyncMock(return_value=existing_status),
        start_new=AsyncMock(return_value="new-instance-id"),
    )


def test_parses_envelope_into_orchestration_input():
    document_guid, orchestration_input = fa._parse_trigger_message(_envelope())
    assert document_guid == DOCUMENT_GUID
    assert orchestration_input == {
        "document_id": DOCUMENT_ID,
        "driver_key": DRIVER_KEY,
        "extracted_blob_url": BLOB_URL,
        "retry": {"first_retry_interval_ms": 30_000, "max_attempts": 3},
    }


def test_retry_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_RETRY_FIRST_INTERVAL_SECONDS", "2.5")
    monkeypatch.setenv("ORCHESTRATION_RETRY_MAX_ATTEMPTS", "5")
    _, orchestration_input = fa._parse_trigger_message(_envelope())
    assert orchestration_input["retry"] == {
        "first_retry_interval_ms": 2500,
        "max_attempts": 5,
    }


def test_null_driver_key_is_preserved_not_defaulted():
    _, orchestration_input = fa._parse_trigger_message(_envelope(driverKey=None))
    assert orchestration_input["driver_key"] is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"documentId": "not-a-uuid"},
        {"documentGuid": "not-a-uuid"},
        {"blobUrl": "not-a-url"},
        {"blobUrl": None},
    ],
)
def test_malformed_envelope_raises(overrides):
    with pytest.raises((ValueError, KeyError)):
        fa._parse_trigger_message(_envelope(**overrides))


def test_no_existing_instance_starts_a_new_one():
    client = _fake_client(existing_status=None)
    asyncio.run(fa.start_document_orchestration(_FakeMessage(_envelope()), client))

    client.start_new.assert_awaited_once_with(
        "DocumentOrchestration",
        instance_id=DOCUMENT_GUID,
        client_input={
            "document_id": DOCUMENT_ID,
            "driver_key": DRIVER_KEY,
            "extracted_blob_url": BLOB_URL,
            "retry": {"first_retry_interval_ms": 30_000, "max_attempts": 3},
        },
    )


@pytest.mark.parametrize(
    "status",
    [
        OrchestrationRuntimeStatus.Pending,
        OrchestrationRuntimeStatus.Running,
        OrchestrationRuntimeStatus.ContinuedAsNew,
        OrchestrationRuntimeStatus.Suspended,
        OrchestrationRuntimeStatus.Completed,
    ],
)
def test_in_flight_or_completed_instance_is_not_restarted(status):
    client = _fake_client(existing_status=SimpleNamespace(runtime_status=status))
    asyncio.run(fa.start_document_orchestration(_FakeMessage(_envelope()), client))
    client.start_new.assert_not_awaited()


@pytest.mark.parametrize(
    "status",
    [
        OrchestrationRuntimeStatus.Failed,
        OrchestrationRuntimeStatus.Terminated,
        OrchestrationRuntimeStatus.Canceled,
    ],
)
def test_terminal_non_completed_instance_is_restarted(status):
    client = _fake_client(existing_status=SimpleNamespace(runtime_status=status))
    asyncio.run(fa.start_document_orchestration(_FakeMessage(_envelope()), client))
    client.start_new.assert_awaited_once()


def test_functions_host_registers_starter_with_all_three_bindings():
    functions = fa.app.get_functions()
    starter = next(
        f for f in functions if f.get_function_name() == "StartDocumentOrchestration"
    )
    bindings = json.loads(starter.get_function_json())["bindings"]
    binding_types = {b["type"] for b in bindings}
    assert {"serviceBusTrigger", "durableClient"} <= binding_types
