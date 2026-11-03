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
