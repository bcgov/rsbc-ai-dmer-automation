"""Unit tests for the Mercury batch API client (faked HTTP GET).

Behaviour specs (GIVEN/WHEN/THEN) for pagination, retry on transient failure,
non-2xx handling, and — critically — that the API key is never logged.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from dmer_common import mercury_client as client_module
from dmer_common.config import MercurySettings
from dmer_common.mercury_client import MercuryApiError, MercuryClient

SECRET_KEY = "super-secret-mercury-key"  # pragma: allowlist secret

SETTINGS = MercurySettings(
    base_url="https://mercury.example.com/api/mercury/documents",
    api_key=SECRET_KEY,
)

PAGE_1 = {
    "value": [{"document_guid": "doc-1"}, {"document_guid": "doc-2"}],
    "nextLink": "https://mercury.example.com/api/mercury/documents?cursor=page2",
}
PAGE_2 = {"value": [{"document_guid": "doc-3"}], "nextLink": None}


class _FakeHttp:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("transient network error")
        page = PAGE_2 if "cursor=page2" in url else PAGE_1
        return 200, json.dumps(page).encode("utf-8")


def test_get_page_returns_records_and_next_url():
    # GIVEN a client with a faked HTTP GET
    http = _FakeHttp()
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching the first page
    page = client.get_page(queue="BOTH", page_size=50)
    # THEN the records and next_url are returned, and the URL was built correctly
    assert [r["document_guid"] for r in page.records] == ["doc-1", "doc-2"]
    assert page.next_url == PAGE_1["nextLink"]
    assert "queue=BOTH" in http.calls[0]["url"]
    assert "page_size=50" in http.calls[0]["url"]


def test_get_page_follows_next_url_directly():
    # GIVEN a client and a next_url from a prior page
    http = _FakeHttp()
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching using next_url THEN it is used as-is, not reconstructed
    page = client.get_page(queue="BOTH", next_url=PAGE_1["nextLink"])
    assert [r["document_guid"] for r in page.records] == ["doc-3"]
    assert page.next_url is None
    assert http.calls[0]["url"] == PAGE_1["nextLink"]


def test_get_page_sends_bearer_auth_header():
    # GIVEN a client
    http = _FakeHttp()
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching a page THEN the Authorization header carries the API key
    client.get_page(queue="BOTH")
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"


def test_get_page_retries_transient_failure():
    # GIVEN a client whose first call fails then succeeds
    http = _FakeHttp(fail_times=1)
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching a page THEN the retry policy recovers
    page = client.get_page(queue="BOTH")
    assert [r["document_guid"] for r in page.records] == ["doc-1", "doc-2"]
    assert len(http.calls) == 2


def test_non_2xx_response_raises_mercury_api_error():
    # GIVEN an HTTP GET that returns a 500
    def http(url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        return 500, b'{"error": "boom"}'

    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching a page THEN a MercuryApiError is raised (after retries exhaust)
    with pytest.raises(MercuryApiError):
        client.get_page(queue="BOTH")


def test_api_key_is_never_logged():
    # GIVEN the module logger writing to an in-memory buffer
    buffer = io.StringIO()
    handler = client_module._log.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)

    http = _FakeHttp()
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN a page is fetched and logged
    client.get_page(queue="BOTH")
    # THEN the emitted JSON never contains the API key
    logged = buffer.getvalue()
    assert SECRET_KEY not in logged
    record = json.loads(logged.strip().splitlines()[-1])
    assert record["record_count"] == 2
    assert record["has_next"] is True


def test_settings_default_from_config(monkeypatch):
    # GIVEN Mercury config in the environment and no explicit settings
    monkeypatch.setenv(
        "MERCURY_API_BASE_URL", "https://m.example.com/api/mercury/documents"
    )
    monkeypatch.setenv("MERCURY_API_KEY", "k")
    http = _FakeHttp()
    # WHEN constructing without settings THEN it loads from config
    client = MercuryClient(http_get=http)
    page = client.get_page(queue="BOTH")
    assert len(page.records) == 2
    assert "m.example.com" in http.calls[0]["url"]
