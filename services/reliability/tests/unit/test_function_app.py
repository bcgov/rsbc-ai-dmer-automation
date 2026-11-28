"""The Outbox Publisher function's wiring (the publisher logic is tested in
dmer_common, against PostgreSQL)."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import reliability.function_app as fa
from dmer_common.db.outbox_publisher import PublisherSettings, PublishSummary


def test_settings_default_to_the_architecture_values(monkeypatch):
    for name in (
        "OUTBOX_BATCH_SIZE",
        "OUTBOX_MAX_ATTEMPTS",
        "OUTBOX_BACKOFF_INITIAL_SECONDS",
        "OUTBOX_BACKOFF_MAX_SECONDS",
        "OUTBOX_LEASE_SECONDS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert fa.publisher_settings() == PublisherSettings()


def test_settings_come_from_app_settings(monkeypatch):
    monkeypatch.setenv("OUTBOX_BATCH_SIZE", "5")
    monkeypatch.setenv("OUTBOX_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("OUTBOX_BACKOFF_INITIAL_SECONDS", "30")
    monkeypatch.setenv("OUTBOX_BACKOFF_MAX_SECONDS", "600")
    monkeypatch.setenv("OUTBOX_LEASE_SECONDS", "120")
    assert fa.publisher_settings() == PublisherSettings(
        batch_size=5,
        max_attempts=3,
        backoff_initial=timedelta(seconds=30),
        backoff_max=timedelta(seconds=600),
        lease=timedelta(seconds=120),
    )


def test_a_run_publishes_with_a_fresh_engine_and_disposes_it(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    publish = AsyncMock(return_value=PublishSummary(documents=1, sent=2))
    monkeypatch.setattr(fa, "publish_due", publish)
    client = object()

    summary = asyncio.run(
        fa.run_publisher(engine_factory=AsyncMock(return_value=engine), client=client)
    )

    assert summary.sent == 2
    args, kwargs = publish.await_args
    assert args == (engine, client)
    assert isinstance(kwargs["settings"], PublisherSettings)
    engine.dispose.assert_awaited_once()


def test_a_run_passes_the_forms_driver_note_reader(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    publish = AsyncMock(return_value=PublishSummary())
    monkeypatch.setattr(fa, "publish_due", publish)

    def reader(document_id):
        return "note"

    asyncio.run(
        fa.run_publisher(
            engine_factory=AsyncMock(return_value=engine),
            client=object(),
            form_driver_note_reader=lambda: reader,
        )
    )
    assert publish.await_args.kwargs["form_driver_note"] is reader


def test_no_form_driver_note_reader_without_a_blob_account(monkeypatch):
    monkeypatch.delenv("BLOB_ACCOUNT_URL", raising=False)
    assert fa.form_driver_note_reader() is None


def test_the_form_driver_note_reader_reads_the_documents_combined_json(monkeypatch):
    from dmer_common import storage

    class _Blob:
        def __init__(self, account_url):
            self.account_url = account_url
            self.urls = []

        def blob_url(self, container, path):
            return f"{self.account_url}/{container}/{path}"

        def download_if_exists(self, url):
            self.urls.append(url)
            return b'{"fields": {"first_name": "Jo", "last_name": "Doe"}}'

    made = []
    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://data.blob")
    monkeypatch.setattr(
        storage, "BlobClient", lambda url: made.append(_Blob(url)) or made[-1]
    )

    reader = fa.form_driver_note_reader()
    assert reader("doc-1") == (
        "Driver details as read from the DMER form: first name Jo, surname Doe."
    )
    assert made[0].urls == ["https://data.blob/extracted-dmer/doc-1/combined.json"]


def test_no_form_driver_note_reader_when_the_blob_client_cant_be_built(monkeypatch):
    from dmer_common import storage

    def broken(url):
        raise RuntimeError("no credential")

    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://data.blob")
    monkeypatch.setattr(storage, "BlobClient", broken)
    assert fa.form_driver_note_reader() is None


def test_the_engine_is_disposed_even_when_the_run_fails(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(
        fa, "publish_due", AsyncMock(side_effect=RuntimeError("db down"))
    )
    try:
        asyncio.run(
            fa.run_publisher(
                engine_factory=AsyncMock(return_value=engine), client=object()
            )
        )
    except RuntimeError:
        pass
    engine.dispose.assert_awaited_once()


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


def test_runtime_storage_uses_managed_identity_when_account_name_set(monkeypatch):
    """In Azure, AzureWebJobsStorage is identity-based (no account key), so
    the reconciliation report upload must authenticate with the app's identity."""
    import azure.identity
    import azure.storage.blob

    credential = object()
    monkeypatch.setenv("AzureWebJobsStorage__accountName", "rsbcstorage")
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    monkeypatch.setattr(azure.storage.blob, "BlobServiceClient", _FakeBlobServiceClient)
    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda: credential)

    client = fa._runtime_blob_service()

    assert client.account_url == "https://rsbcstorage.blob.core.windows.net"
    assert client.credential is credential
    assert client.connection_string is None


def test_runtime_storage_falls_back_to_connection_string_locally(monkeypatch):
    import azure.storage.blob

    monkeypatch.delenv("AzureWebJobsStorage__accountName", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage", "UseDevelopmentStorage=true")
    monkeypatch.setattr(azure.storage.blob, "BlobServiceClient", _FakeBlobServiceClient)

    client = fa._runtime_blob_service()

    assert client.connection_string == "UseDevelopmentStorage=true"
