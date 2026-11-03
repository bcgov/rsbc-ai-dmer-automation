"""Unit tests for BlobClient URL construction and URI parsing (no network I/O)."""

from __future__ import annotations

import pytest
from dmer_common.storage import BlobClient, containers

ACCOUNT = "https://stdmerdevcac001.blob.core.windows.net"


def _client() -> BlobClient:
    # A fake static credential avoids Managed Identity lookup; no network call is
    # made until an operation runs, so URL helpers are safe to exercise.
    return BlobClient(ACCOUNT, credential="fake-key")


def test_blob_url_composes_account_container_path():
    # GIVEN a client and a container/path
    client = _client()
    # WHEN building the blob URL
    url = client.blob_url(containers.extracted_dmer(), "doc-1/combined.json")
    # THEN it is the full https URL
    assert url == f"{ACCOUNT}/extracted-dmer/doc-1/combined.json"


def test_split_uri_round_trips_with_blob_url():
    # GIVEN a URL produced by blob_url
    client = _client()
    url = client.blob_url(containers.extracted_dmer(), "doc-1/ocr.json")
    # WHEN split back into (container, path)
    container, path = client._split_uri(url)
    # THEN the components match
    assert container == "extracted-dmer"
    assert path == "doc-1/ocr.json"


@pytest.mark.parametrize(
    "bad",
    [
        "https://acct.blob.core.windows.net/only-container",
        "https://acct.blob.core.windows.net/",
        "not-a-url",
    ],
)
def test_split_uri_rejects_malformed(bad):
    # GIVEN a malformed blob URL
    client = _client()
    # WHEN splitting THEN it raises
    with pytest.raises(ValueError):
        client._split_uri(bad)


class _FakeBlob:
    """Stands in for an SDK blob client: records where it points, returns bytes."""

    def __init__(self, account_url, container, blob):
        self.target = (account_url, container, blob)

    def download_blob(self):
        return self

    def readall(self):
        return b"%PDF-fake"


def test_download_same_account_uses_this_clients_account(monkeypatch):
    # GIVEN a blob URL in this client's own account
    client = _client()
    seen = []
    monkeypatch.setattr(
        client._service,
        "get_blob_client",
        lambda container, blob: seen.append((container, blob))
        or _FakeBlob(ACCOUNT, container, blob),
    )
    # WHEN downloading
    data = client.download(f"{ACCOUNT}/raw/2026/09/doc.pdf")
    # THEN the own-account service client is used
    assert data == b"%PDF-fake"
    assert seen == [("raw", "2026/09/doc.pdf")]


def test_download_other_account_targets_the_urls_account(monkeypatch):
    # GIVEN a blob URL in a different storage account (Ingest writes source PDFs
    # to rsbcstorage, not this client's account)
    import dmer_common.storage.client as mod

    client = _client()
    made = []

    def fake_sdk(*, account_url, container_name, blob_name, credential):
        made.append((account_url, container_name, blob_name, credential))
        return _FakeBlob(account_url, container_name, blob_name)

    monkeypatch.setattr(mod, "_SdkBlobClient", fake_sdk)
    monkeypatch.setattr(
        client._service,
        "get_blob_client",
        lambda **_: pytest.fail("must not look in this client's own account"),
    )
    # WHEN downloading
    data = client.download(
        "https://rsbcstorage.blob.core.windows.net/raw-dmer/2026/09/doc.pdf"
    )
    # THEN it reads from the URL's account, with this client's credential
    assert data == b"%PDF-fake"
    assert made == [
        (
            "https://rsbcstorage.blob.core.windows.net",
            "raw-dmer",
            "2026/09/doc.pdf",
            "fake-key",
        )
    ]


@pytest.mark.parametrize(
    "bad",
    [
        "http://rsbcstorage.blob.core.windows.net/raw-dmer/doc.pdf",  # not https
        "https://evil.example.com/raw-dmer/doc.pdf",  # not Azure Blob
        "https://rsbcstorage.blob.core.windows.net.evil.com/raw-dmer/doc.pdf",
    ],
)
def test_download_refuses_non_azure_blob_urls(bad):
    # GIVEN a URL that isn't an https Azure Blob endpoint
    client = _client()
    # WHEN downloading THEN it raises before any request carries the token
    with pytest.raises(ValueError):
        client.download(bad)
