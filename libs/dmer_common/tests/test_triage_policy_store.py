"""Tests for dmer_common.triage.policy_store: the procedures text, generated
once per PDF version and kept in the triage-policy container."""

from __future__ import annotations

import hashlib
import threading
from datetime import date

import pytest
from azure.core.exceptions import ResourceNotFoundError
from dmer_common.triage import (
    PolicyTextError,
    PolicyUnavailableError,
    load_policy,
    policy_store,
)

ACCOUNT = "https://stdmertest.blob.core.windows.net"
PDF_URL = f"{ACCOUNT}/rules/active/triage_sort_procedures.pdf"
TEXT = "TRIAGE SORT PROCEDURES FOR DMERS (updated 12Jun2026) - converted from the PDF.\n..."


class FakeBlob:
    """Blob storage in memory: content and an ETag that changes on every write."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.calls: list[tuple[str, str]] = []
        self._version = 0

    def put(self, url: str, data: bytes) -> None:
        self._version += 1
        self.blobs[url] = data
        self.etags[url] = f'"0x{self._version}"'

    def blob_url(self, container: str, path: str) -> str:
        return f"{ACCOUNT}/{container}/{path}"

    def etag(self, uri: str) -> str:
        self.calls.append(("etag", uri))
        if uri not in self.etags:
            raise ResourceNotFoundError("missing")
        return self.etags[uri]

    def download(self, uri: str) -> bytes:
        self.calls.append(("download", uri))
        if uri not in self.blobs:
            raise ResourceNotFoundError("missing")
        return self.blobs[uri]

    def download_if_exists(self, uri: str) -> bytes | None:
        self.calls.append(("download_if_exists", uri))
        return self.blobs.get(uri)

    def upload_bytes(self, container, path, data, *, content_settings=None) -> str:
        url = self.blob_url(container, path)
        self.calls.append(("upload", url))
        self.put(url, data)
        return url


@pytest.fixture(autouse=True)
def _fresh_worker():
    policy_store.clear_cache()
    yield
    policy_store.clear_cache()


@pytest.fixture
def builds(monkeypatch) -> list[bytes]:
    """Stand-in for policy_text.build: records each PDF it converts."""
    built: list[bytes] = []

    def fake_build(pdf: bytes) -> str:
        built.append(pdf)
        return TEXT + f"\n(from {len(pdf)} bytes)"

    monkeypatch.setattr(policy_store, "build", fake_build)
    return built


def _text_url(pdf: bytes) -> str:
    return f"{ACCOUNT}/triage-policy/{hashlib.sha256(pdf).hexdigest()}.txt"


def test_a_new_pdf_is_converted_once_and_its_text_stored(builds):
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF v1")

    policy = load_policy(blob)

    assert builds == [b"%PDF v1"]
    assert blob.blobs[_text_url(b"%PDF v1")] == policy.text.encode()
    assert policy.pdf_sha256 == hashlib.sha256(b"%PDF v1").hexdigest()
    assert policy.updated == date(2026, 6, 12)


def test_an_unchanged_pdf_reads_only_its_etag(builds):
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF v1")
    first = load_policy(blob)
    blob.calls.clear()

    assert load_policy(blob) is first
    assert blob.calls == [("etag", PDF_URL)]
    assert len(builds) == 1


def test_another_worker_reads_the_stored_text_instead_of_converting(builds):
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF v1")
    stored = load_policy(blob).text
    policy_store.clear_cache()  # a different worker

    policy = load_policy(blob)

    assert policy.text == stored
    assert len(builds) == 1


def test_a_replaced_pdf_gets_its_own_text(builds):
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF v1")
    first = load_policy(blob)
    blob.put(PDF_URL, b"%PDF version 2")

    second = load_policy(blob)

    assert builds == [b"%PDF v1", b"%PDF version 2"]
    assert second.pdf_sha256 != first.pdf_sha256
    assert _text_url(b"%PDF v1") in blob.blobs  # the old version's text is kept
    assert _text_url(b"%PDF version 2") in blob.blobs


def test_a_missing_pdf_is_unavailable(builds):
    with pytest.raises(PolicyUnavailableError):
        load_policy(FakeBlob())
    assert builds == []


def test_a_text_that_fails_verification_is_neither_stored_nor_used(monkeypatch):
    def failing_build(pdf: bytes) -> str:
        raise PolicyTextError("Converted text does not match the PDF on 1 check(s)")

    monkeypatch.setattr(policy_store, "build", failing_build)
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF broken")

    with pytest.raises(PolicyTextError):
        load_policy(blob)
    assert _text_url(b"%PDF broken") not in blob.blobs
    with pytest.raises(PolicyTextError):  # tried again next time, never cached
        load_policy(blob)


def test_concurrent_callers_on_one_worker_convert_once(monkeypatch):
    started, release = threading.Event(), threading.Event()
    built: list[bytes] = []

    def slow_build(pdf: bytes) -> str:
        built.append(pdf)
        started.set()
        release.wait(timeout=5)
        return TEXT

    monkeypatch.setattr(policy_store, "build", slow_build)
    blob = FakeBlob()
    blob.put(PDF_URL, b"%PDF v1")
    results: list = []
    threads = [
        threading.Thread(target=lambda: results.append(load_policy(blob)))
        for _ in range(4)
    ]
    threads[0].start()
    started.wait(timeout=5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(timeout=5)

    assert len(built) == 1
    assert len(results) == 4 and all(r is results[0] for r in results)
