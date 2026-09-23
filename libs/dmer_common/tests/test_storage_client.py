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
