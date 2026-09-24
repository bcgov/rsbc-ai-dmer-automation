"""Unit tests for the pure/logic-heavy parts of function_app.py (the Ingest
stage -- see that module's own docstring and docs/development/stages/01-ingest.md):
timestamp parsing, the shared message envelope, the per-record driver/dedup
decision in `_upsert_document_and_driver`, message publishing, the replay
guard in `dmer_ingest`, and the webhook scaffold's 501 response. Everything
that talks to Postgres, Blob Storage, Service Bus, or the Mercury API itself
is monkeypatched out or replaced with an in-memory fake -- nothing here
needs Azurite, a real Postgres, or network access (see
../local/mock_mercury_api.py and ../integration/README.md for the tests that
do).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime

import function_app as fa


def test_parse_mercury_datetime_none_and_empty_return_none():
    assert fa._parse_mercury_datetime(None) is None
    assert fa._parse_mercury_datetime("") is None


def test_parse_mercury_datetime_parses_iso8601():
    assert fa._parse_mercury_datetime("2024-10-03T07:11:00Z") == datetime.fromisoformat(
        "2024-10-03T07:11:00Z"
    )


def test_mercury_auth_headers_uses_api_key_env(monkeypatch):
    monkeypatch.setenv("MERCURY_API_KEY", "test-key-123")
    assert fa._mercury_auth_headers() == {"Authorization": "Bearer test-key-123"}


def test_envelope_defaults_blob_url_to_none_for_dmer_ingest():
    """The dmer-ingest message has nothing to point at yet (module docstring:
    Ingest reads document_url from dmer_document, not the queue message)."""
    envelope = fa._envelope(
        document_id="doc-1", document_guid="DMER-1", driver_key=None, blob_url=None
    )
    assert envelope["schemaVersion"] == "1.0"
    assert envelope["documentId"] == "doc-1"
    assert envelope["documentGuid"] == "DMER-1"
    assert envelope["driverKey"] is None
    assert envelope["blobUrl"] is None
    assert envelope["attempt"] == 1
    assert envelope["enqueuedAt"].endswith("Z")


def test_envelope_carries_blob_url_for_dmer_raw():
    envelope = fa._envelope(
        document_id="doc-1",
        document_guid="DMER-1",
        driver_key="drv-1",
        blob_url="https://x/raw-dmer/2024/10/DMER-1.pdf",
    )
    assert envelope["driverKey"] == "drv-1"
    assert envelope["blobUrl"] == "https://x/raw-dmer/2024/10/DMER-1.pdf"


class _FakeDriverRepo:
    """Records every upsert() call; returns a fixed driver_key."""

    def __init__(self):
        self.calls: list[dict] = []

    async def upsert(self, licence_number, **kwargs):
        self.calls.append({"licence_number": licence_number, **kwargs})
        return f"driver-key-for-{licence_number}"


class _FakeDocRepo:
    """Records every upsert_received() call; returns a fixed document_id."""

    def __init__(self):
        self.calls: list[dict] = []

    async def upsert_received(self, **kwargs):
        self.calls.append(kwargs)
        return "document-id-1"


def test_upsert_document_and_driver_uses_null_driver_key_when_no_driver():
    """The core "not all DMERs have a driver attached" contract."""
    record = {
        "document_guid": "A1",
        "document_url": "https://x/A1.pdf",
        "driver": None,
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    document_id, driver_key = asyncio.run(run())

    assert document_id == "document-id-1"
    assert driver_key is None
    assert driver_repo.calls == []
    assert doc_repo.calls[0]["driver_key"] is None


def test_upsert_document_and_driver_upserts_driver_when_licence_present():
    record = {
        "document_guid": "A2",
        "document_url": "https://x/A2.pdf",
        "driver": {
            "licence_number": "123456",
            "driver_id": "mercury-driver-9",
            "first_name": "Jane",
            "last_name": "Doe",
        },
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    _document_id, driver_key = asyncio.run(run())

    assert driver_key == "driver-key-for-123456"
    assert driver_repo.calls == [
        {
            "licence_number": "123456",
            "mercury_driver_id": "mercury-driver-9",
            "first_name": "Jane",
            "last_name": "Doe",
            "last_synced_at": driver_repo.calls[0]["last_synced_at"],
        }
    ]
    assert doc_repo.calls[0]["driver_key"] == "driver-key-for-123456"


def test_upsert_document_and_driver_skips_driver_upsert_without_licence_number():
    """A driver object with no licence_number attached is treated the same
    as no driver at all -- licence_number is the only key driver_repo.upsert
    can act on."""
    record = {
        "document_guid": "A3",
        "document_url": "https://x/A3.pdf",
        "driver": {"first_name": "Jane"},
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    _document_id, driver_key = asyncio.run(run())

    assert driver_key is None
    assert driver_repo.calls == []


def test_upsert_document_and_driver_prefers_dps_queue_over_queue():
    record = {
        "document_guid": "A4",
        "document_url": "https://x/A4.pdf",
        "driver": None,
        "dps_queue": "General",
        "queue": "Team - Intake",
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    asyncio.run(run())

    assert doc_repo.calls[0]["queue"] == "General"


def test_upsert_document_and_driver_falls_back_to_queue_when_dps_queue_absent():
    record = {
        "document_guid": "A5",
        "document_url": "https://x/A5.pdf",
        "driver": None,
        "queue": "Team - Intake",
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    asyncio.run(run())

    assert doc_repo.calls[0]["queue"] == "Team - Intake"


def test_upsert_document_and_driver_extracts_mercury_case_id():
    record = {
        "document_guid": "A6",
        "document_url": "https://x/A6.pdf",
        "driver": None,
        "case": {"case_id": "C1234"},
    }
    doc_repo = _FakeDocRepo()
    driver_repo = _FakeDriverRepo()

    async def run():
        return await fa._upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )

    asyncio.run(run())

    assert doc_repo.calls[0]["mercury_case_id"] == "C1234"


def test_publish_message_sends_expected_queue_message_id_and_body(monkeypatch):
    """Verifies `_publish_message`'s duplicate-detection contract
    (message-contracts.md): the Service Bus MessageId is the caller-supplied
    id, and the body is whatever dict the caller passed, unmodified -- all
    without needing a real Service Bus connection.
    """
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE_FQDN", "sb-test.servicebus.windows.net")
    sent = {}

    class FakeSender:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_messages(self, message):
            sent["message"] = message

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_queue_sender(self, queue_name):
            sent["queue_name"] = queue_name
            return FakeSender()

    class FakeMessage:
        def __init__(self, body, message_id):
            self.body = body
            self.message_id = message_id

    monkeypatch.setattr(fa, "ServiceBusClient", FakeClient)
    monkeypatch.setattr(fa, "ServiceBusMessage", FakeMessage)
    monkeypatch.setattr(fa, "DefaultAzureCredential", lambda: object())

    fa._publish_message("dmer-ingest", "DMER-1", {"documentGuid": "DMER-1"})

    assert sent["queue_name"] == "dmer-ingest"
    assert sent["message"].message_id == "DMER-1"
    assert json.loads(sent["message"].body) == {"documentGuid": "DMER-1"}


def test_dmer_webhook_returns_501_scaffold_response():
    """Mercury doesn't emit new-DMER events yet (question M-8) -- this
    endpoint is registered but must do nothing but say so."""
    response = fa.dmer_webhook(None)

    assert response.status_code == 501
    assert b"not yet emit new-DMER events" in response.get_body()


class _ReplayGuardRow:
    def __init__(self, pipeline_status):
        self.pipeline_status = pipeline_status
        self.document_url = "https://x/whatever.pdf"
        self.attempt_count = 0


class _FakeDocRepoForReplayGuard:
    def __init__(self, row):
        self._row = row

    async def get_by_id(self, _document_id):
        return self._row


class _FakeEngine:
    async def dispose(self):
        pass


def _make_ingest_message(document_id="doc-1", document_guid="DMER-1"):
    class FakeMessage:
        def get_body(self):
            return json.dumps(
                {
                    "documentId": document_id,
                    "documentGuid": document_guid,
                    "driverKey": None,
                }
            ).encode("utf-8")

    return FakeMessage()


def test_dmer_ingest_replay_guard_noops_when_already_past_received(monkeypatch):
    """A redelivered dmer-ingest message for a document already past
    RECEIVED must not re-download the PDF or re-publish to dmer-raw
    (01-ingest.md's idempotency requirement) -- Service Bus's own
    duplicate-detection window only covers the *first* delivery, so this
    in-code guard is what protects a message redelivered after it already
    succeeded.
    """
    row = _ReplayGuardRow(pipeline_status="DOWNLOADED")

    async def fake_get_async_engine():
        return _FakeEngine()

    monkeypatch.setattr(fa, "_get_async_engine", fake_get_async_engine)
    monkeypatch.setattr(
        fa, "DmerDocumentRepository", lambda engine: _FakeDocRepoForReplayGuard(row)
    )

    download_calls = []
    publish_calls = []
    monkeypatch.setattr(
        fa, "_download_source_pdf", lambda *a, **k: download_calls.append((a, k))
    )
    monkeypatch.setattr(
        fa, "_publish_message", lambda *a, **k: publish_calls.append((a, k))
    )

    asyncio.run(fa.dmer_ingest(_make_ingest_message()))

    assert download_calls == []
    assert publish_calls == []


def test_dmer_ingest_logs_error_and_noops_when_document_row_missing(monkeypatch):
    async def fake_get_async_engine():
        return _FakeEngine()

    monkeypatch.setattr(fa, "_get_async_engine", fake_get_async_engine)
    monkeypatch.setattr(
        fa, "DmerDocumentRepository", lambda engine: _FakeDocRepoForReplayGuard(None)
    )

    download_calls = []
    monkeypatch.setattr(
        fa, "_download_source_pdf", lambda *a, **k: download_calls.append((a, k))
    )

    # Must not raise even though there's nothing to process -- the replay
    # guard's "no dmer_document row" branch is a logged no-op, not an error.
    asyncio.run(fa.dmer_ingest(_make_ingest_message()))

    assert download_calls == []
