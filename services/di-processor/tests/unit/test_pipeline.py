"""Unit tests for the extraction pipeline orchestration (Stages A-E).

All I/O is faked; asserts cover stage sequencing, idempotent no-op, status
transitions, persistence of blob URLs, downstream publish, and failure routing.
Requirements: 3.2, 3.4, 4.3, 4.4, 4.5, 5.3, 6.3, 6.4, 6.6, 7.1, 7.2.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from di_processor.pipeline import Pipeline, PipelineConfig
from dmer_common.db import DocumentStatus
from dmer_common.dto import RawDmerMessage
from PIL import Image

pytestmark = pytest.mark.asyncio


# --- fakes -----------------------------------------------------------------


class FakeRepo:
    def __init__(self, initial=None):
        self._status = initial
        self.transitions: list[DocumentStatus] = []
        self.uris: dict[str, str] = {}

    async def get_status(self, document_id):
        return self._status

    async def upsert_status(self, document_id, correlation_id, status, **kw):
        self.transitions.append(status)
        self._status = status
        for k in (
            "initial_extraction_uri",
            "combined_extraction_uri",
            "failure_reason",
        ):
            if kw.get(k) is not None:
                self.uris[k] = kw[k]


class FakeBlob:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []

    def download(self, uri):
        return b"%PDF-fake-bytes"

    def upload_json(self, container, path, obj):
        self.uploads.append((container, path))
        return f"https://acct.blob.core.windows.net/{container}/{path}"


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, envelope):
        self.published.append(envelope)


class FakeDICustom:
    """Returns a DIResult-like object with one document + fields."""

    def analyze(self, model_id, document, *, pages=None):
        from dmer_common.doc_intelligence import DIResult

        return DIResult(
            content="",
            documents=[
                {
                    "fields": {
                        "dl_number": {"valueString": "01234567", "confidence": 0.99}
                    }
                }
            ],
        )


def _raw_message(**over):
    base = {
        "message_id": "m-1",
        "correlation_id": "case-1",
        "schema_version": "1.0",
        "source_system": "mercury-webhook",
        "document_id": "doc-1",
        "mercury_case_id": "case-1",
        "document_uri": "https://acct.blob.core.windows.net/raw/doc-1.pdf",
        "received_at": datetime(2026, 8, 5, tzinfo=UTC),
        "payload": {},
    }
    base.update(over)
    return RawDmerMessage(**base)


def _pipeline(monkeypatch, *, repo=None, blob=None, publisher=None):
    """Build a Pipeline with extraction functions monkeypatched to fakes."""
    import di_processor.pipeline as pl

    # Stub the extraction steps so no real render/OCR/LLM runs.
    monkeypatch.setattr(
        pl.render, "render_cropped_page", lambda b: Image.new("RGB", (10, 10))
    )
    monkeypatch.setattr(pl.render, "PAGE_NUMBER", 1, raising=False)
    monkeypatch.setattr(pl.splitter, "create_tiles", lambda img: [])
    monkeypatch.setattr(
        pl.di_ocr,
        "ocr_tiles",
        lambda client, tiles, **kw: {"pages": [{"page_number": 1, "rows": []}]},
    )

    from di_processor.extraction.schemas import (
        Confidence,
        HandwrittenExtraction,
        HandwrittenField,
        Source,
    )
    from dmer_common.dto import EXTRACTED_DMER_SCHEMA_VERSION  # noqa: F401

    def fake_reconstruct(openai, image, ocr):
        return HandwrittenExtraction(
            fields={
                "endocrine.HbA1C": HandwrittenField(
                    value="6.5", confidence=Confidence.HIGH, source=Source.BOTH
                )
            },
            uncertain_fields=[],
        )

    monkeypatch.setattr(pl.llm_reconstruct, "reconstruct", fake_reconstruct)

    return Pipeline(
        config=PipelineConfig(custom_model_id="rsbc-ocr-dmer-v9"),
        blob=blob or FakeBlob(),
        di_custom=FakeDICustom(),
        di_ocr_client=object(),
        openai=object(),
        repository=repo or FakeRepo(),
        publisher=publisher or FakePublisher(),
    )


# --- tests -----------------------------------------------------------------


async def test_happy_path_transitions_and_publishes(monkeypatch):
    repo = FakeRepo()
    blob = FakeBlob()
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, blob=blob, publisher=publisher)

    await pipeline.run(_raw_message())

    # status walked the full happy path
    assert repo.transitions == [
        DocumentStatus.RECEIVED,
        DocumentStatus.EXTRACTING,
        DocumentStatus.SECTIONING,
        DocumentStatus.COMBINING,
        DocumentStatus.COMBINED,
        DocumentStatus.PUBLISHED,
    ]
    # artifacts persisted to the two containers
    containers = {c for c, _ in blob.uploads}
    assert "extracted-dmer" in containers
    assert "combined-extracted-dmer" in containers
    # published exactly one extracted-dmer message referencing the combined result
    assert len(publisher.published) == 1
    msg = publisher.published[0]
    assert msg.document_id == "doc-1"
    assert msg.combined_result_uri.endswith("combined.json")
    assert msg.sha256_hash  # hash computed


async def test_idempotent_noop_when_already_published(monkeypatch):
    repo = FakeRepo(initial=DocumentStatus.PUBLISHED)
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, publisher=publisher)

    await pipeline.run(_raw_message())

    # no transitions written, nothing published
    assert repo.transitions == []
    assert publisher.published == []


async def test_failure_records_failed_status_and_reraises(monkeypatch):
    repo = FakeRepo()

    class BoomBlob(FakeBlob):
        def download(self, uri):
            raise RuntimeError("blob down")

    pipeline = _pipeline(monkeypatch, repo=repo, blob=BoomBlob())

    with pytest.raises(RuntimeError):
        await pipeline.run(_raw_message())

    # failure recorded so the consumer can dead-letter
    assert DocumentStatus.FAILED in repo.transitions
    assert repo.uris.get("failure_reason") == "RuntimeError"
