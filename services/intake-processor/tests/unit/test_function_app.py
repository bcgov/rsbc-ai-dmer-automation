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
import pytest
from dmer_common.mercury_client import MercuryPage


def test_parse_mercury_datetime_none_and_empty_return_none():
    assert fa._parse_mercury_datetime(None) is None
    assert fa._parse_mercury_datetime("") is None


def test_parse_mercury_datetime_parses_iso8601():
    assert fa._parse_mercury_datetime("2024-10-03T07:11:00Z") == datetime.fromisoformat(
        "2024-10-03T07:11:00Z"
    )


def test_envelope_defaults_blob_url_to_none_for_dmer_ingest():
    """The dmer-ingest message has nothing to point at yet (Ingest downloads
    the file from Mercury by the document guid)."""
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
            "licence_number": "1234567",
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

    assert driver_key == "driver-key-for-01234567"
    assert driver_repo.calls == [
        {
            "licence_number": "01234567",  # canonical 8 digits
            "mercury_driver_id": "mercury-driver-9",
            "first_name": "Jane",
            "last_name": "Doe",
            "last_synced_at": driver_repo.calls[0]["last_synced_at"],
        }
    ]
    assert doc_repo.calls[0]["driver_key"] == "driver-key-for-01234567"


@pytest.mark.parametrize("licence", ["00000000", "0000000000", "123456"])
def test_placeholder_or_invalid_mercury_licence_is_stored_without_a_driver(licence):
    # 00000000 is Mercury's placeholder driver for a DMER it couldn't map; an
    # invalid licence is bad data. Either way: no driver, and no failed poll.
    record = {
        "document_guid": "A9",
        "document_url": "https://x/A9.pdf",
        "driver": {
            "licence_number": licence,
            "driver_id": "placeholder-driver",
            "first_name": "",
            "last_name": "DFC",
        },
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
    assert doc_repo.calls[0]["driver_key"] is None


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


# ---------------------------------------------------------------------------
# Page Poller: one GET dmers page, then mark it AI Processed
# ---------------------------------------------------------------------------


class _FakeMercury:
    """GET dmers returns *records*; every call is recorded in *events*."""

    def __init__(self, records, events, *, mark_fails=False):
        self.records, self.events, self.mark_fails = records, events, mark_fails

    def get_page(self, *, page_size):
        self.events.append(("get_page", page_size))
        return MercuryPage(records=self.records, has_more=False)

    def set_ai_processed(self, guids):
        self.events.append(("set_ai_processed", list(guids)))
        if self.mark_fails:
            raise RuntimeError("Mercury unavailable")


class _FakeCheckpointRepo:
    def __init__(self, events):
        self.events = events

    async def update(self, source, **kwargs):
        self.events.append(("checkpoint", source, kwargs["last_cursor"]))


def _poll(monkeypatch, records, *, mark_fails=False):
    events: list = []
    doc_repo = _FakeDocRepo()
    monkeypatch.setattr(fa, "DmerDocumentRepository", lambda engine: doc_repo)
    monkeypatch.setattr(fa, "DriverRepository", lambda engine: _FakeDriverRepo())
    monkeypatch.setattr(
        fa, "PollCheckpointRepository", lambda engine: _FakeCheckpointRepo(events)
    )
    monkeypatch.setattr(
        fa,
        "_publish_message",
        lambda queue, message_id, body: events.append(("publish", message_id)),
    )
    monkeypatch.setenv("MERCURY_PAGE_SIZE", "50")
    mercury = _FakeMercury(records, events, mark_fails=mark_fails)
    error = None
    try:
        asyncio.run(fa._run_poll(object(), mercury))
    except RuntimeError as exc:
        error = exc
    return events, doc_repo, error


_DMER = {"document_guid": "G1", "document_type": "DMER", "document_url": "u1"}
_HRDIR = {
    "document_guid": "G2",
    "document_type": "HRDIR High Risk Driving Incident Report",
    "document_url": None,
}
_DMER_2 = {"document_guid": "G3", "document_type": "dmer", "document_url": "u3"}


def test_poll_stores_and_queues_only_the_dmers(monkeypatch):
    events, doc_repo, _ = _poll(monkeypatch, [_DMER, _HRDIR, _DMER_2])
    assert [c["document_guid"] for c in doc_repo.calls] == ["G1", "G3"]
    assert [e[1] for e in events if e[0] == "publish"] == ["G1", "G3"]


def test_poll_marks_every_record_ai_processed_after_queuing_the_dmers(monkeypatch):
    # Non-DMERs are marked too, so GET dmers stops returning them.
    events, _, error = _poll(monkeypatch, [_DMER, _HRDIR, _DMER_2])
    assert error is None
    assert [e[0] for e in events] == [
        "get_page",
        "publish",
        "publish",
        "set_ai_processed",
        "checkpoint",
    ]
    assert ("set_ai_processed", ["G1", "G2", "G3"]) in events
    assert ("get_page", 50) in events
    assert ("checkpoint", "BACKLOG", None) in events  # claim-based: no cursor


def test_a_failed_mark_fails_the_poll_so_the_next_tick_gets_the_page_again(
    monkeypatch,
):
    events, _, error = _poll(monkeypatch, [_DMER], mark_fails=True)
    assert error is not None
    assert [e[0] for e in events] == ["get_page", "publish", "set_ai_processed"]


def test_an_empty_page_stores_nothing_and_marks_nothing(monkeypatch):
    events, doc_repo, _ = _poll(monkeypatch, [])
    assert doc_repo.calls == []
    assert ("set_ai_processed", []) in events  # the client skips an empty call


class _FakeBlobServiceClient:
    def __init__(self, account_url, credential=None):
        self.account_url = account_url
        self.credential = credential
        self.connection_string = None

    @classmethod
    def from_connection_string(cls, connection_string):
        client = cls(None)
        client.connection_string = connection_string
        return client


def test_runtime_blob_service_uses_managed_identity_when_account_name_set(monkeypatch):
    """In Azure, AzureWebJobsStorage is identity-based (no account key), so
    the raw-dmer upload must authenticate with the app's identity."""
    credential = object()
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "rsbcstorage")
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.setattr(fa, "BlobServiceClient", _FakeBlobServiceClient)
    monkeypatch.setattr(fa, "DefaultAzureCredential", lambda: credential)

    client = fa._runtime_blob_service()

    assert client.account_url == "https://rsbcstorage.blob.core.windows.net"
    assert client.credential is credential
    assert client.connection_string is None


def test_runtime_blob_service_falls_back_to_connection_string_locally(monkeypatch):
    monkeypatch.delenv("AzureWebJobsStorage__accountName", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setattr(fa, "BlobServiceClient", _FakeBlobServiceClient)

    client = fa._runtime_blob_service()

    assert client.connection_string == "UseDevelopmentStorage=true"


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


def test_engine_keeps_bound_values_out_of_error_text(monkeypatch):
    """SQLAlchemy error text, which reaches logs and error_detail, must not
    include bound values such as a driver's name or licence number."""
    monkeypatch.setenv("POSTGRES_HOST", "localhost")
    monkeypatch.setenv("POSTGRES_USER", "app")
    monkeypatch.setenv("POSTGRES_PASSWORD", "unused")
    engine = asyncio.run(fa._get_async_engine())
    assert engine.sync_engine.hide_parameters is True

    # What the setting does, on an in-memory database: the statement is in the
    # error text, the value is not.
    from sqlalchemy import create_engine, text

    sync = create_engine("sqlite://", hide_parameters=True)
    try:
        with sync.connect() as conn:
            conn.execute(
                text("INSERT INTO missing_table VALUES (:v)"), {"v": "DL1234567"}
            )
    except Exception as exc:  # noqa: BLE001
        assert "missing_table" in str(exc)
        assert "DL1234567" not in str(exc)
    else:
        raise AssertionError("expected an error")


class _IngestRow:
    def __init__(self):
        self.pipeline_status = "RECEIVED"
        self.document_guid = "26946190-c3b4-ee11-b84f-00505683d8c0"
        self.document_url = None  # Mercury's documentUrl is not used to download
        self.attempt_count = 0


class _FakeDocRepoForIngest:
    def __init__(self, row):
        self._row = row
        self.downloaded = []

    async def get_by_id(self, _document_id):
        return self._row

    async def mark_downloaded(self, document_id, *, raw_blob_url, now):
        self.downloaded.append((document_id, raw_blob_url))


class _FakeStageRuns:
    def __init__(self):
        self.events = []

    async def start(self, **kwargs):
        self.events.append("start")
        return 1

    async def succeed(self, run_id, **kwargs):
        self.events.append("succeed")

    async def fail(self, run_id, **kwargs):
        self.events.append("fail")


def test_dmer_ingest_downloads_from_mercury_by_document_guid(monkeypatch):
    """Ingest fetches the PDF by the row's document guid (Mercury's
    GET dmers/{documentRecordId}/files), not from documentUrl -- which is a
    path inside Mercury, and may be empty -- then stores and hands it on."""
    row = _IngestRow()
    docs = _FakeDocRepoForIngest(row)
    runs = _FakeStageRuns()

    async def fake_get_async_engine():
        return _FakeEngine()

    downloads, uploads, published = [], [], []
    monkeypatch.setattr(fa, "_get_async_engine", fake_get_async_engine)
    monkeypatch.setattr(fa, "DmerDocumentRepository", lambda engine: docs)
    monkeypatch.setattr(fa, "DmerStageRunRepository", lambda engine: runs)
    monkeypatch.setattr(
        fa, "_download_source_pdf", lambda guid: downloads.append(guid) or b"%PDF-1.7"
    )
    monkeypatch.setattr(
        fa,
        "_upload_raw_dmer_blob",
        lambda content, guid, container: (
            uploads.append((content, guid)) or f"https://raw/{guid}.pdf"
        ),
    )
    monkeypatch.setattr(
        fa, "_publish_message", lambda queue, *a, **k: published.append(queue)
    )

    asyncio.run(fa.dmer_ingest(_make_ingest_message(document_guid=row.document_guid)))

    assert downloads == [row.document_guid]
    assert uploads == [(b"%PDF-1.7", row.document_guid)]
    assert docs.downloaded == [("doc-1", f"https://raw/{row.document_guid}.pdf")]
    assert published == ["dmer-raw"]
    assert runs.events == ["start", "succeed"]


def test_download_source_pdf_uses_the_mercury_client(monkeypatch):
    calls = []

    class FakeClient:
        def download_dmer(self, guid):
            calls.append(guid)
            return b"%PDF-1.7"

    monkeypatch.setattr(fa, "MercuryClient", FakeClient)
    assert fa._download_source_pdf("G1") == b"%PDF-1.7"
    assert calls == ["G1"]
