"""Unit tests for the extraction pipeline orchestration (Stages A-E).

All I/O is faked; asserts cover stage sequencing, idempotent no-op, pipeline
status transitions, persistence of blob artifacts under the single
``extracted-dmer`` container, downstream publish, and failure routing to
MANUAL_REVIEW.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from di_processor.failures import FailureCode, PipelineFailure
from di_processor.pipeline import Pipeline, PipelineConfig
from dmer_common.db import PipelineStage, PipelineStatus
from dmer_common.db.dmer_document import StaleStatusError, _validated_status
from dmer_common.dto import EXTRACTED_EVENT, RawMessage, event_message_id
from PIL import Image

pytestmark = pytest.mark.asyncio

FIXTURES = Path(__file__).parent.parent / "fixtures"


# --- fakes -----------------------------------------------------------------


class FakeRepo:
    """In-memory repo with the real compare-and-set + state-machine rules.

    ``interleave`` simulates another worker: ``{target: status}`` sets the row to
    ``status`` just before this run writes ``target``, as if a concurrent
    worker had written first.
    """

    def __init__(self, initial=None, stage=None, interleave=None):
        self._interleave = dict(interleave or {})
        self._status = initial
        self.stage = stage  # current_stage; only changed when a write passes one
        self.transitions: list[PipelineStatus] = []
        self.stage_writes: list[tuple[PipelineStatus, object]] = []
        self.blob_urls: list[str] = []

    async def get_status(self, document_id):
        return self._status

    async def upsert_status(
        self,
        document_id,
        status,
        *,
        expected,
        document_guid=None,
        stage=None,
    ):
        if status in self._interleave:  # another worker writes first
            self._status = self._interleave.pop(status)
        _validated_status(expected, status)
        if expected is None and stage is None:
            raise ValueError("stage is required on the initial insert")
        if expected != self._status:  # compare-and-set
            raise StaleStatusError(document_id, expected, self._status)
        self.transitions.append(status)
        self.stage_writes.append((status, stage))
        if stage is not None:
            self.stage = stage
        self._status = status


class FakeExtractionRepo:
    def __init__(self):
        self.records = []

    async def upsert(self, record):
        self.records.append(record)


class FakeStageRunRepo:
    """Records start/succeed/fail calls; optionally fails on succeed."""

    def __init__(self, fail_on_succeed=False):
        self.started: list[tuple[str, str, str | None]] = []
        self.succeeded: list[tuple[int, str | None]] = []
        self.failed: list[tuple[int, str, str | None]] = []
        self._fail_on_succeed = fail_on_succeed

    async def start(self, *, document_id, stage, model_version=None, **_):
        self.started.append((document_id, stage.value, model_version))
        return len(self.started)

    async def succeed(self, run_id, *, output_blob_url=None):
        if self._fail_on_succeed:
            raise RuntimeError("db down")
        self.succeeded.append((run_id, output_blob_url))

    async def fail(self, run_id, *, error_code, error_detail=None):
        self.failed.append((run_id, error_code, error_detail))


class FakeBlob:
    def __init__(self):
        self.uploads: list[tuple[str, str]] = []
        self.objects: dict[str, dict] = {}

    def download(self, uri):
        return b"%PDF-fake-bytes"

    def blob_url(self, container, path):
        return f"https://acct.blob.core.windows.net/{container}/{path}"

    def upload_json(self, container, path, obj):
        self.uploads.append((container, path))
        self.objects[path] = obj
        return f"https://acct.blob.core.windows.net/{container}/{path}"


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, envelope):
        self.published.append(envelope)


class FakeDICustom:
    """Returns a DIResult-like object with one document + fields."""

    def __init__(self, raw=None):
        self._raw = raw  # optional full analyze result (e.g. a real fixture)

    def analyze(self, model_id, document, *, pages=None):
        from dmer_common.doc_intelligence import DIResult

        if self._raw is not None:
            return DIResult.from_sdk(self._raw)
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
        "schema_version": "1.0",
        "document_id": "doc-1",
        "document_guid": "123e4567-e89b-12d3-a456-426614174000",
        "driver_key": None,
        "blob_url": "https://acct.blob.core.windows.net/raw-dmer/doc-1.pdf",
        "attempt": 1,
        "enqueued_at": datetime(2026, 8, 5, tzinfo=UTC),
    }
    base.update(over)
    return RawMessage(**base)


def _pipeline(
    monkeypatch,
    *,
    repo=None,
    blob=None,
    publisher=None,
    extractions=None,
    di_custom=None,
    stage_runs=None,
    prompt_version=None,
):
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
        config=PipelineConfig(
            custom_model_id="rsbc-ocr-dmer-v9", prompt_version=prompt_version
        ),
        blob=blob or FakeBlob(),
        di_custom=di_custom or FakeDICustom(),
        di_ocr_client=object(),
        openai=object(),
        repository=repo or FakeRepo(),
        extraction_repository=extractions or FakeExtractionRepo(),
        stage_run_repository=stage_runs or FakeStageRunRepo(),
        publisher=publisher or FakePublisher(),
    )


# --- tests -----------------------------------------------------------------


async def test_happy_path_transitions_and_publishes(monkeypatch):
    repo = FakeRepo()
    blob = FakeBlob()
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, blob=blob, publisher=publisher)

    await pipeline.run(_raw_message())

    # pipeline_status walked the extraction slice of the lifecycle
    assert repo.transitions == [
        PipelineStatus.RECEIVED,
        PipelineStatus.DOWNLOADED,
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
    ]
    # all artifacts persisted to the single extracted-dmer container
    containers = {c for c, _ in blob.uploads}
    assert containers == {"extracted-dmer"}
    paths = {p for _, p in blob.uploads}
    assert any(p.endswith("top_level.json") for p in paths)
    assert any(p.endswith("ocr.json") for p in paths)
    assert any(p.endswith("handwritten.json") for p in paths)
    assert any(p.endswith("combined.json") for p in paths)
    # published exactly one dmer-extracted message pointing at the combined result
    assert len(publisher.published) == 1
    msg = publisher.published[0]
    assert msg.document_id == "doc-1"
    assert msg.document_guid == "123e4567-e89b-12d3-a456-426614174000"
    assert msg.blob_url.endswith("combined.json")


async def test_extracted_is_persisted_before_publish(monkeypatch):
    # GIVEN a publisher that snapshots the document status at publish time
    repo = FakeRepo()

    class StatusCapturingPublisher(FakePublisher):
        def publish(self, envelope):
            self.status_at_publish = repo._status
            super().publish(envelope)

    publisher = StatusCapturingPublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, publisher=publisher)

    # WHEN the pipeline runs
    await pipeline.run(_raw_message())

    # THEN EXTRACTED (with the pointer) was already durable when publishing
    assert publisher.status_at_publish is PipelineStatus.EXTRACTED


async def test_replay_at_extracted_republishes_stored_pointer(monkeypatch):
    # GIVEN a crash after EXTRACTED was persisted but before the publish
    stored = "https://acct.blob.core.windows.net/extracted-dmer/doc-1/combined.json"
    repo = FakeRepo(initial=PipelineStatus.EXTRACTED)
    blob = FakeBlob()
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, blob=blob, publisher=publisher)

    # WHEN the message is redelivered
    await pipeline.run(_raw_message())

    # THEN the stored pointer is re-published without re-running extraction
    assert repo.transitions == []
    assert blob.uploads == []
    assert len(publisher.published) == 1
    msg = publisher.published[0]
    assert msg.blob_url == stored
    # same deterministic extraction-event id as the first publish, so
    # downstream dedups the repeat — and never the upstream dmer-raw id
    assert msg.message_id == event_message_id(EXTRACTED_EVENT, "doc-1")
    assert msg.message_id != "m-1"


async def test_noop_when_past_extraction(monkeypatch):
    # GIVEN downstream has already advanced the document
    repo = FakeRepo(initial=PipelineStatus.NORMALIZED)
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, publisher=publisher)

    await pipeline.run(_raw_message())

    # THEN no transitions written, nothing published
    assert repo.transitions == []
    assert publisher.published == []


async def test_starts_from_ingest_owned_downloaded_row(monkeypatch):
    # GIVEN Ingest has already written RECEIVED -> DOWNLOADED
    repo = FakeRepo(initial=PipelineStatus.DOWNLOADED)
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, publisher=publisher)

    # WHEN extraction runs
    await pipeline.run(_raw_message())

    # THEN it does not rewrite Ingest's statuses and completes extraction
    assert repo.transitions == [
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
    ]
    assert len(publisher.published) == 1


async def test_advances_row_left_at_received(monkeypatch):
    # GIVEN a row Ingest inserted but did not advance past RECEIVED
    repo = FakeRepo(initial=PipelineStatus.RECEIVED)
    pipeline = _pipeline(monkeypatch, repo=repo)

    await pipeline.run(_raw_message())

    # THEN di-processor supplies DOWNLOADED, without re-inserting RECEIVED
    assert repo.transitions == [
        PipelineStatus.DOWNLOADED,
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
    ]


async def test_redelivery_mid_extraction_reprocesses(monkeypatch):
    # GIVEN a previous attempt died while EXTRACTING (e.g. lock lost)
    repo = FakeRepo(initial=PipelineStatus.EXTRACTING)
    publisher = FakePublisher()
    pipeline = _pipeline(monkeypatch, repo=repo, publisher=publisher)

    # WHEN the message is redelivered
    await pipeline.run(_raw_message())

    # THEN extraction re-enters EXTRACTING and completes (no MANUAL_REVIEW)
    assert repo.transitions == [
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
    ]
    assert len(publisher.published) == 1


async def test_failure_routes_to_manual_review_and_reraises(monkeypatch):
    repo = FakeRepo()

    class BoomBlob(FakeBlob):
        def download(self, uri):
            raise RuntimeError("blob down")

    pipeline = _pipeline(monkeypatch, repo=repo, blob=BoomBlob())

    with pytest.raises(PipelineFailure):
        await pipeline.run(_raw_message())

    # failure routed to MANUAL_REVIEW so the consumer can dead-letter
    assert PipelineStatus.MANUAL_REVIEW in repo.transitions


async def test_cutoff_flags_persisted_for_real_cut_off_sample(monkeypatch):
    # GIVEN the custom model returns the real bottom-cut-off DMER's layout
    fixture = FIXTURES / "top_level_raw_cutoff_bottom.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    extractions = FakeExtractionRepo()
    blob = FakeBlob()
    publisher = FakePublisher()
    pipeline = _pipeline(
        monkeypatch,
        blob=blob,
        publisher=publisher,
        extractions=extractions,
        di_custom=FakeDICustom(raw),
    )

    # WHEN the pipeline runs
    await pipeline.run(_raw_message())

    # THEN the flags land on the dmer_extraction row ...
    assert len(extractions.records) == 1
    record = extractions.records[0]
    assert record.document_id == "doc-1"
    assert (record.has_header, record.has_signature, record.is_cutoff) == (
        True,
        False,
        True,
    )
    # the trimmed fixture carries no dl_number field -> no licence recorded
    assert record.licence_number_read is None
    # ... and in the combined extraction blob
    combined = blob.objects["doc-1/combined.json"]
    assert combined["cutoff"]["is_cutoff"] is True
    # AND a cut-off document is still extracted and published (Decision
    # Gateway needs its fields for duplicate comparison)
    assert len(publisher.published) == 1


async def test_licence_read_persisted_normalized(monkeypatch):
    # GIVEN the custom model read dl_number "01234567" at high confidence
    extractions = FakeExtractionRepo()
    pipeline = _pipeline(monkeypatch, extractions=extractions)

    # WHEN the pipeline runs
    await pipeline.run(_raw_message())

    # THEN the canonical licence lands on the dmer_extraction row
    assert extractions.records[0].licence_number_read == "01234567"


# --- audit trail (dmer_stage_run) -------------------------------------------


async def test_stage_run_opened_and_succeeded_with_combined_blob(monkeypatch):
    # GIVEN a normal run
    stage_runs = FakeStageRunRepo()
    publisher = FakePublisher()
    pipeline = _pipeline(
        monkeypatch, stage_runs=stage_runs, publisher=publisher, prompt_version="v3"
    )

    # WHEN it completes
    await pipeline.run(_raw_message())

    # THEN one EXTRACT run is opened with both model and prompt versions ...
    assert stage_runs.started == [("doc-1", "EXTRACT", "di=rsbc-ocr-dmer-v9;prompt=v3")]
    # ... and closed SUCCEEDED pointing at the combined blob that was published
    assert stage_runs.succeeded == [(1, publisher.published[0].blob_url)]
    assert stage_runs.failed == []


async def test_unversioned_prompt_is_recorded_explicitly(monkeypatch):
    stage_runs = FakeStageRunRepo()
    await _pipeline(monkeypatch, stage_runs=stage_runs).run(_raw_message())
    assert stage_runs.started[0][2] == "di=rsbc-ocr-dmer-v9;prompt=unversioned"


async def test_stage_run_failed_with_error_type_on_error(monkeypatch):
    # GIVEN the source download fails after the run was opened
    class BoomBlob(FakeBlob):
        def download(self, uri):
            raise RuntimeError("blob down: https://acct/raw-dmer/doc-1.pdf")

    stage_runs = FakeStageRunRepo()
    repo = FakeRepo()
    pipeline = _pipeline(monkeypatch, repo=repo, blob=BoomBlob(), stage_runs=stage_runs)

    with pytest.raises(PipelineFailure):
        await pipeline.run(_raw_message())

    # THEN the run is closed FAILED with the step's code + safe detail only
    assert stage_runs.failed == [(1, "SOURCE_DOWNLOAD_FAILED", "error=RuntimeError")]
    assert stage_runs.succeeded == []
    assert PipelineStatus.MANUAL_REVIEW in repo.transitions


async def test_audit_write_failure_after_publish_keeps_document_published(
    monkeypatch,
):
    # GIVEN the SUCCEEDED write fails after a successful publish
    stage_runs = FakeStageRunRepo(fail_on_succeed=True)
    repo = FakeRepo()
    publisher = FakePublisher()
    pipeline = _pipeline(
        monkeypatch, repo=repo, publisher=publisher, stage_runs=stage_runs
    )

    # WHEN the pipeline runs THEN it does not raise ...
    await pipeline.run(_raw_message())

    # ... and the published document is NOT routed to MANUAL_REVIEW
    assert len(publisher.published) == 1
    assert repo.transitions[-1] is PipelineStatus.EXTRACTED
    assert PipelineStatus.MANUAL_REVIEW not in repo.transitions
    assert stage_runs.failed == []


async def test_republish_and_noop_replays_write_no_stage_run(monkeypatch):
    # GIVEN replays that do not re-run extraction
    for repo in (
        FakeRepo(initial=PipelineStatus.EXTRACTED),
        FakeRepo(initial=PipelineStatus.NORMALIZED),
    ):
        stage_runs = FakeStageRunRepo()
        await _pipeline(monkeypatch, repo=repo, stage_runs=stage_runs).run(
            _raw_message()
        )
        # THEN no stage-run row is written
        assert stage_runs.started == []


async def test_redelivery_mid_extraction_opens_a_new_attempt(monkeypatch):
    # GIVEN a previous attempt died while EXTRACTING
    stage_runs = FakeStageRunRepo()
    repo = FakeRepo(initial=PipelineStatus.EXTRACTING)

    await _pipeline(monkeypatch, repo=repo, stage_runs=stage_runs).run(_raw_message())

    # THEN a new run is opened (the repository numbers it and abandons the old)
    assert len(stage_runs.started) == 1
    assert len(stage_runs.succeeded) == 1


# --- current_stage ----------------------------------------------------------


async def test_current_stage_moves_to_normalize_on_extracted(monkeypatch):
    # GIVEN a normal run
    repo = FakeRepo()
    await _pipeline(monkeypatch, repo=repo).run(_raw_message())

    # THEN EXTRACT while extracting, NORMALIZE with EXTRACTED
    assert repo.stage_writes == [
        (PipelineStatus.RECEIVED, PipelineStage.EXTRACT),
        (PipelineStatus.DOWNLOADED, PipelineStage.EXTRACT),
        (PipelineStatus.EXTRACTING, PipelineStage.EXTRACT),
        (PipelineStatus.EXTRACTED, PipelineStage.NORMALIZE),
    ]
    assert repo.stage is PipelineStage.NORMALIZE


async def test_ingest_owned_row_moves_to_normalize(monkeypatch):
    # GIVEN Ingest left the row at DOWNLOADED / EXTRACT
    repo = FakeRepo(initial=PipelineStatus.DOWNLOADED, stage=PipelineStage.EXTRACT)
    await _pipeline(monkeypatch, repo=repo).run(_raw_message())
    assert repo.stage is PipelineStage.NORMALIZE


async def test_manual_review_leaves_current_stage_unchanged(monkeypatch):
    # GIVEN extraction fails after the run started
    class BoomBlob(FakeBlob):
        def download(self, uri):
            raise RuntimeError("blob down")

    repo = FakeRepo()
    with pytest.raises(PipelineFailure):
        await _pipeline(monkeypatch, repo=repo, blob=BoomBlob()).run(_raw_message())

    # THEN the MANUAL_REVIEW write passes no stage: position stays EXTRACT
    assert repo.stage_writes[-1] == (PipelineStatus.MANUAL_REVIEW, None)
    assert repo.stage is PipelineStage.EXTRACT


async def test_failed_republish_keeps_normalize_stage(monkeypatch):
    # GIVEN an EXTRACTED document (stage NORMALIZE) whose re-publish fails
    class DownPublisher(FakePublisher):
        def publish(self, envelope):
            raise ConnectionError("service bus down")

    repo = FakeRepo(initial=PipelineStatus.EXTRACTED, stage=PipelineStage.NORMALIZE)
    with pytest.raises(PipelineFailure):
        await _pipeline(monkeypatch, repo=repo, publisher=DownPublisher()).run(
            _raw_message()
        )

    # THEN it goes to MANUAL_REVIEW without being moved back to EXTRACT
    assert repo.transitions == [PipelineStatus.MANUAL_REVIEW]
    assert repo.stage is PipelineStage.NORMALIZE


# --- failure codes (item 2) -------------------------------------------------

FAKE_PII = "licence 01234567; dx: epilepsy"  # stands in for extracted PII/clinical text


class _HttpError(Exception):
    """Mimics an Azure / OpenAI SDK error carrying an HTTP status."""

    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


def _boom(*_args, **_kwargs):
    raise RuntimeError(FAKE_PII)


async def _run_expecting_failure(pipeline):
    with pytest.raises(PipelineFailure) as info:
        await pipeline.run(_raw_message())
    return info.value


def _failing_blob(method):
    class Blob(FakeBlob):
        pass

    setattr(Blob, method, lambda self, *a, **k: _boom())
    return Blob()


@pytest.mark.parametrize(
    ("code", "build"),
    [
        (
            FailureCode.SOURCE_DOWNLOAD_FAILED,
            lambda mp: {"blob": _failing_blob("download")},
        ),
        (
            FailureCode.DI_CUSTOM_MODEL_FAILED,
            lambda mp: {"di_custom": type("DI", (), {"analyze": _boom})()},
        ),
        (
            FailureCode.ARTIFACT_WRITE_FAILED,
            lambda mp: {"blob": _failing_blob("upload_json")},
        ),
        (
            FailureCode.DB_WRITE_FAILED,
            lambda mp: {"extractions": type("X", (), {"upsert": _async_boom})()},
        ),
        (
            FailureCode.PUBLISH_FAILED,
            lambda mp: {"publisher": type("P", (), {"publish": _boom})()},
        ),
    ],
    ids=lambda v: v.value if isinstance(v, FailureCode) else "",
)
async def test_each_step_failure_is_reported_with_its_code(monkeypatch, code, build):
    # GIVEN one step's dependency fails with a message carrying extracted values
    stage_runs = FakeStageRunRepo()
    repo = FakeRepo()
    pipeline = _pipeline(
        monkeypatch, repo=repo, stage_runs=stage_runs, **build(monkeypatch)
    )

    # WHEN the pipeline runs
    failure = await _run_expecting_failure(pipeline)

    # THEN it is classified by the failing step, with a PII-free detail
    assert failure.code is code
    assert failure.dead_letter_reason == code.value
    assert failure.safe_detail == "error=RuntimeError"
    assert stage_runs.failed == [(1, code.value, "error=RuntimeError")]
    assert PipelineStatus.MANUAL_REVIEW in repo.transitions


async def _async_boom(*_args, **_kwargs):
    raise RuntimeError(FAKE_PII)


@pytest.mark.parametrize(
    ("code", "target", "attr"),
    [
        (FailureCode.PDF_UNREADABLE, "render", "render_cropped_page"),
        (FailureCode.OCR_FAILED, "di_ocr", "ocr_tiles"),
        (FailureCode.UNEXPECTED, "merge", "merge"),
    ],
    ids=["PDF_UNREADABLE", "OCR_FAILED", "UNEXPECTED"],
)
async def test_processing_step_failures_are_classified(monkeypatch, code, target, attr):
    import di_processor.pipeline as pl

    stage_runs = FakeStageRunRepo()
    pipeline = _pipeline(monkeypatch, stage_runs=stage_runs)
    monkeypatch.setattr(getattr(pl, target), attr, _boom)

    failure = await _run_expecting_failure(pipeline)

    assert failure.code is code
    assert stage_runs.failed[0][1] == code.value


async def test_db_read_failure_is_classified(monkeypatch):
    # GIVEN the initial status read fails (database unavailable)
    class Repo(FakeRepo):
        async def get_status(self, document_id):
            raise RuntimeError(FAKE_PII)

    stage_runs = FakeStageRunRepo()
    failure = await _run_expecting_failure(
        _pipeline(monkeypatch, repo=Repo(), stage_runs=stage_runs)
    )

    # THEN DB_READ_FAILED; no stage run was opened
    assert failure.code is FailureCode.DB_READ_FAILED
    assert stage_runs.started == []


async def test_illegal_status_transition_is_classified(monkeypatch):
    # GIVEN a document already in MANUAL_REVIEW is redelivered
    repo = FakeRepo(initial=PipelineStatus.MANUAL_REVIEW, stage=PipelineStage.EXTRACT)
    failure = await _run_expecting_failure(_pipeline(monkeypatch, repo=repo))

    # THEN the EXTRACTING write is rejected as an illegal transition
    assert failure.code is FailureCode.INVALID_STATUS_TRANSITION


async def test_http_status_is_carried_in_the_detail(monkeypatch):
    # GIVEN Document Intelligence throttles (HTTP 429) past the retry budget
    class Throttled:
        def analyze(self, *_a, **_k):
            raise _HttpError(FAKE_PII, 429)

    stage_runs = FakeStageRunRepo()
    failure = await _run_expecting_failure(
        _pipeline(monkeypatch, di_custom=Throttled(), stage_runs=stage_runs)
    )

    assert failure.code is FailureCode.DI_CUSTOM_MODEL_FAILED
    assert failure.safe_detail == "error=_HttpError; http_status=429"


async def test_failure_text_never_reaches_detail_or_logs(monkeypatch, caplog):
    # GIVEN a failure whose message contains extracted licence / clinical text
    stage_runs = FakeStageRunRepo()
    pipeline = _pipeline(
        monkeypatch, blob=_failing_blob("download"), stage_runs=stage_runs
    )

    with caplog.at_level("DEBUG"):
        failure = await _run_expecting_failure(pipeline)

    # THEN it appears nowhere we record: detail, str(failure), stage run, logs
    assert "01234567" not in failure.safe_detail
    assert "01234567" not in str(failure)
    assert all("01234567" not in (row[2] or "") for row in stage_runs.failed)
    assert "01234567" not in caplog.text
    assert "epilepsy" not in caplog.text
    # (the original stays on __cause__ for local debugging only)
    assert FAKE_PII in str(failure.__cause__)


# --- dmer-extracted message id ----------------------------------------------


async def test_extracted_message_id_is_not_the_upstream_raw_id(monkeypatch):
    publisher = FakePublisher()
    await _pipeline(monkeypatch, publisher=publisher).run(_raw_message())

    msg = publisher.published[0]
    assert msg.message_id != "m-1"
    assert msg.message_id == event_message_id(EXTRACTED_EVENT, "doc-1")
    # the upstream link is the document id, not a shared message id
    assert msg.document_id == "doc-1"


async def test_extracted_message_id_is_stable_across_upstream_redeliveries(
    monkeypatch,
):
    # GIVEN the same document arrives twice with different dmer-raw message ids
    # (e.g. Ingest or the sweeper re-published it)
    first_pub, second_pub = FakePublisher(), FakePublisher()
    await _pipeline(monkeypatch, publisher=first_pub).run(
        _raw_message(message_id="m-1")
    )
    await _pipeline(monkeypatch, publisher=second_pub).run(
        _raw_message(message_id="m-2", attempt=2)
    )

    # THEN both publishes carry the same extraction-event id, so downstream
    # recognizes the second as a repeat
    assert first_pub.published[0].message_id == second_pub.published[0].message_id


# --- compare-and-set races (two workers on one document) ---------------------


async def test_lost_race_at_extracted_stops_quietly(monkeypatch):
    # GIVEN another worker writes EXTRACTED just before this run does
    repo = FakeRepo(
        initial=PipelineStatus.DOWNLOADED,
        stage=PipelineStage.EXTRACT,
        interleave={PipelineStatus.EXTRACTED: PipelineStatus.EXTRACTED},
    )
    stage_runs = FakeStageRunRepo()
    publisher = FakePublisher()
    pipeline = _pipeline(
        monkeypatch, repo=repo, stage_runs=stage_runs, publisher=publisher
    )

    # WHEN this run tries to write EXTRACTED THEN it does not raise ...
    await pipeline.run(_raw_message())

    # ... does not publish, does not route to MANUAL_REVIEW, keeps the winner's
    # status, and closes its own stage run as STALE_STATUS
    assert publisher.published == []
    assert PipelineStatus.MANUAL_REVIEW not in repo.transitions
    assert repo._status is PipelineStatus.EXTRACTED
    assert stage_runs.failed[0][1] == "STALE_STATUS"
    assert "expected=EXTRACTING" in stage_runs.failed[0][2]
    assert "actual=EXTRACTED" in stage_runs.failed[0][2]


async def test_status_cannot_move_backwards_under_a_race(monkeypatch):
    # GIVEN this run read DOWNLOADED, but another worker has since reached
    # EXTRACTED — the old read-validate-write would have rewritten EXTRACTING
    repo = FakeRepo(
        initial=PipelineStatus.DOWNLOADED,
        stage=PipelineStage.EXTRACT,
        interleave={PipelineStatus.EXTRACTING: PipelineStatus.EXTRACTED},
    )
    await _pipeline(monkeypatch, repo=repo).run(_raw_message())

    # THEN the EXTRACTING write is rejected and the status stays EXTRACTED
    assert repo._status is PipelineStatus.EXTRACTED
    assert PipelineStatus.EXTRACTING not in repo.transitions


async def test_failure_does_not_override_another_workers_result(monkeypatch):
    # GIVEN this run fails (download) while another worker completes the
    # document (EXTRACTED) before this run's MANUAL_REVIEW write
    class BoomBlob(FakeBlob):
        def download(self, uri):
            raise RuntimeError("blob down")

    repo = FakeRepo(
        initial=PipelineStatus.DOWNLOADED,
        stage=PipelineStage.EXTRACT,
        interleave={PipelineStatus.MANUAL_REVIEW: PipelineStatus.EXTRACTED},
    )
    # WHEN it fails THEN it stops quietly (no dead-letter of this duplicate)
    await _pipeline(monkeypatch, repo=repo, blob=BoomBlob()).run(_raw_message())

    # AND the winner's EXTRACTED is not overwritten with MANUAL_REVIEW
    assert repo._status is PipelineStatus.EXTRACTED
    assert PipelineStatus.MANUAL_REVIEW not in repo.transitions


async def test_lost_race_on_bootstrap_insert_stops_quietly(monkeypatch):
    # GIVEN no row when read, but another worker inserts it first
    repo = FakeRepo(interleave={PipelineStatus.RECEIVED: PipelineStatus.RECEIVED})
    stage_runs = FakeStageRunRepo()
    publisher = FakePublisher()

    await _pipeline(
        monkeypatch, repo=repo, stage_runs=stage_runs, publisher=publisher
    ).run(_raw_message())

    # THEN nothing is published and no stage run was opened
    assert publisher.published == []
    assert stage_runs.started == []


async def test_same_status_reentry_still_allowed(monkeypatch):
    # GIVEN a redelivery finds the document mid-EXTRACTING (no concurrent write)
    repo = FakeRepo(initial=PipelineStatus.EXTRACTING, stage=PipelineStage.EXTRACT)
    publisher = FakePublisher()
    await _pipeline(monkeypatch, repo=repo, publisher=publisher).run(_raw_message())

    # THEN EXTRACTING -> EXTRACTING is accepted and extraction completes
    assert repo.transitions == [PipelineStatus.EXTRACTING, PipelineStatus.EXTRACTED]
    assert len(publisher.published) == 1
