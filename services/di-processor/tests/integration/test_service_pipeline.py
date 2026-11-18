"""Service-level integration test: consume -> all stages -> persist -> publish.

Exercises the real service wiring (``main.build_application`` + ``main.run``) and
the full extraction pipeline code path (render -> tile -> DI OCR remap -> LLM
reconstruct -> merge) end-to-end. Per the task, the emulated infrastructure
(Blob, Service Bus, PostgreSQL) is represented by boundary fakes that honour the
same contracts the Azurite / Service Bus emulator / PostgreSQL clients expose,
and the external systems (Document Intelligence and Azure OpenAI) are mocked at
their SDK boundary via the injectable ``dmer_common`` clients.

Covers the happy path (Requirements 3.*, 4.*, 5.*, 6.*, 7.*) and the
dead-letter-on-failure path (Requirement 9.2). Written GIVEN/WHEN/THEN.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import urlparse

import fitz
import pytest
from di_processor.config import Settings
from di_processor.main import build_application, run
from dmer_common.db import DmerDocumentRepository, PipelineStatus
from dmer_common.db.dmer_document import StaleStatusError, _validated_status
from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.messaging import InMemoryIdempotencyStore, ServiceBusPublisher
from dmer_common.openai_client import OpenAIClient
from dmer_common.storage import BlobClient

# --- test fixtures / helpers ----------------------------------------------


def _make_pdf() -> bytes:
    """A minimal one-page DMER-like PDF for the render step."""
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 72), "DMER integration test page")
    data = doc.tobytes()
    doc.close()
    return data


def _settings() -> Settings:
    return Settings(
        app_configuration_endpoint="https://appcfg.example",
        service_bus_namespace_fqdn="sb.example.servicebus.windows.net",
        dmer_raw_queue="dmer-raw",
        dmer_extracted_queue="dmer-extracted",
        postgres_host="pg.example",
        postgres_user="id-rsbc-dmer-di-processor-test",
        blob_account_url="https://acct.blob.core.windows.net",
        doc_intelligence_endpoint="https://di.example",
        custom_model_id="rsbc-ocr-dmer-v9",
        prompt_version="v-test",
        health_port=0,
    )


# --- emulated-infra boundary fakes ----------------------------------------


class FakeBlobService:
    """In-memory stand-in for the Azurite-backed BlobServiceClient boundary.

    Implements only the surface ``dmer_common.storage.BlobClient`` calls
    (``get_blob_client(...).download_blob().readall()`` / ``.upload_blob(...)``),
    so the real BlobClient path-splitting/URL logic runs unchanged.
    """

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], bytes] = {}

    def seed(self, account_url: str, container: str, path: str, data: bytes) -> str:
        self.store[(container, path)] = data
        return f"{account_url}/{container}/{path}"

    def get_blob_client(self, *, container: str, blob: str) -> Any:
        store = self.store

        class _Blob:
            def download_blob(self) -> Any:
                data = store[(container, blob)]

                class _D:
                    def readall(self) -> bytes:
                        return data

                return _D()

            def upload_blob(
                self, data: bytes, *, overwrite: bool = True, content_settings=None
            ) -> None:
                store[(container, blob)] = data

        return _Blob()


class FakeSbReceiver:
    """Iterable Service Bus receiver over a fixed set of messages.

    Records settlement so the test can assert complete vs dead-letter.
    """

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages
        self.completed: list[Any] = []
        self.dead_lettered: list[tuple[Any, str | None]] = []
        self.abandoned: list[Any] = []

    def __iter__(self):
        return iter(self._messages)

    def complete_message(self, message: Any) -> None:
        self.completed.append(message)

    def abandon_message(self, message: Any) -> None:
        self.abandoned.append(message)

    def dead_letter_message(
        self, message: Any, *, reason=None, error_description=None
    ) -> None:
        self.dead_lettered.append((message, reason))


class FakeSbSender:
    """Captures published Service Bus messages."""

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def send_messages(self, message: Any) -> None:
        self.sent.append(message)


class _SbMessage:
    """A received message whose body is the JSON envelope bytes."""

    def __init__(self, envelope: dict[str, Any]) -> None:
        self.body = json.dumps(envelope).encode("utf-8")


def _sb_body(message: Any) -> bytes:
    """Decode a ServiceBusMessage body (bytes or a generator of byte chunks)."""
    body = message.body
    if isinstance(body, (bytes, bytearray)):
        return bytes(body)
    if isinstance(body, str):
        return body.encode("utf-8")
    return b"".join(bytes(chunk) for chunk in body)


class InMemoryRepository(DmerDocumentRepository):
    """DmerDocumentRepository over an in-memory store (PostgreSQL stand-in).

    Reuses the repository's own ``_validated_status`` state-machine check so the
    same transition rules are exercised as against a live database.
    """

    def __init__(self) -> None:  # bypass AsyncEngine requirement
        self._rows: dict[str, dict[str, Any]] = {}
        self.transitions: list[PipelineStatus] = []

    async def get_status(self, document_id: str) -> PipelineStatus | None:
        row = self._rows.get(document_id)
        return PipelineStatus(row["pipeline_status"]) if row else None

    async def upsert_status(
        self,
        document_id: str,
        status: PipelineStatus,
        *,
        expected: PipelineStatus | None,
        document_guid: str | None = None,
        stage: Any = None,
    ) -> None:
        target = _validated_status(expected, status)
        if expected is None and not document_guid:
            raise ValueError("document_guid is required on the initial insert")
        if expected is None and stage is None:
            raise ValueError("stage is required on the initial insert")
        current = await self.get_status(document_id)
        if expected != current:  # compare-and-set, like the real repository
            raise StaleStatusError(document_id, expected, current)
        row = self._rows.setdefault(document_id, {})
        row.update(
            id=document_id,
            pipeline_status=target.value,
        )
        if stage is not None:
            row["current_stage"] = stage.value
        if document_guid is not None:
            row["document_guid"] = document_guid
        self.transitions.append(target)


# --- mocked external SDKs (DI + OpenAI) ------------------------------------


class _Poller:
    def __init__(self, result: Any) -> None:
        self._result = result

    def result(self) -> Any:
        return self._result


class FakeDISdk:
    """Mock DI SDK client returning a canned AnalyzeResult-like dict.

    Custom-model calls (Stage A) return one document with fields; prebuilt-read
    calls (Stage B tiles) return a page with one recognized line.
    """

    def begin_analyze_document(self, *, model_id: str, body: Any, **kwargs: Any) -> Any:
        if model_id == "prebuilt-read":
            result = {
                "content": "180/90",
                "pages": [
                    {
                        "words": [
                            {
                                "content": "180/90",
                                "span": {"offset": 0, "length": 6},
                                "confidence": 0.95,
                            }
                        ],
                        "lines": [
                            {
                                "content": "180/90",
                                "polygon": [10, 10, 60, 10, 60, 25, 10, 25],
                                "spans": [{"offset": 0, "length": 6}],
                            }
                        ],
                    }
                ],
            }
        else:  # custom top-level model
            result = {
                "content": "",
                "documents": [
                    {
                        "fields": {
                            "dl_number": {
                                "valueString": "01234567",
                                "confidence": 0.99,
                            },
                            "diabetes": {
                                "valueSelectionMark": "selected",
                                "confidence": 0.97,
                            },
                        }
                    }
                ],
            }
        return _Poller(result)


class FakeOpenAISdk:
    """Mock Azure OpenAI SDK returning a conformant handwritten-fields JSON."""

    def __init__(self, field_keys: tuple[str, ...]) -> None:
        fields = {
            k: {"value": "", "confidence": "low", "source": "none", "notes": ""}
            for k in field_keys
        }
        # Populate one field with an agreed value.
        if field_keys:
            fields[field_keys[0]] = {
                "value": "180/90",
                "confidence": "high",
                "source": "both",
                "notes": "",
            }
        self._payload = json.dumps({"fields": fields, "uncertain_fields": []})
        self.chat = self  # allow client.chat.completions.create
        self.completions = self

    def create(self, *, model: str, messages: list[Any], **kwargs: Any) -> Any:
        payload = self._payload

        class _Msg:
            content = payload

        class _Choice:
            message = _Msg()

        class _Resp:
            choices: ClassVar = [_Choice()]

        return _Resp()


class InMemoryExtractionRepository:
    """ExtractionRepository stand-in: keeps the latest row per document."""

    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    async def upsert(self, record: Any) -> None:
        self.rows[record.document_id] = record


class InMemoryStageRunRepository:
    """StageRunRepository stand-in: keeps each run's final state."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []

    async def start(
        self, *, document_id: str, stage: Any, model_version=None, **_
    ) -> int:
        self.runs.append(
            {
                "document_id": document_id,
                "stage": stage.value,
                "status": "RUNNING",
                "model_version": model_version,
            }
        )
        return len(self.runs)

    async def succeed(self, run_id: int, *, output_blob_url=None) -> None:
        self.runs[run_id - 1].update(
            status="SUCCEEDED", output_blob_url=output_blob_url
        )

    async def fail(self, run_id: int, *, error_code: str, error_detail=None) -> None:
        self.runs[run_id - 1].update(
            status="FAILED", error_code=error_code, error_detail=error_detail
        )


def _build_app(
    *,
    receiver,
    blob_service,
    repo,
    sender,
    fail_download=False,
    extraction_repo=None,
    stage_run_repo=None,
    idempotency_store=None,
):
    from di_processor.extraction.sanitize import load_field_keys

    settings = _settings()
    # Build the real BlobClient but inject the emulated blob-service boundary
    # (Azurite stand-in) without opening a live SDK client.
    blob = BlobClient.__new__(BlobClient)
    blob._account_url = settings.blob_account_url.rstrip("/")
    blob._service = blob_service

    di = DocumentIntelligenceClient(sdk_client=FakeDISdk())
    openai = OpenAIClient(
        sdk_client=FakeOpenAISdk(load_field_keys()),
        settings=type(
            "S",
            (),
            {
                "deployment": "gpt-5.1",
                "endpoint": "x",
                "api_key": "x",
                "api_version": "x",
            },
        )(),
    )
    return build_application(
        settings,
        blob=blob,
        di_custom=di,
        di_ocr=di,
        openai=openai,
        repository=repo,
        extraction_repository=extraction_repo or InMemoryExtractionRepository(),
        stage_run_repository=stage_run_repo or InMemoryStageRunRepository(),
        idempotency_store=idempotency_store or InMemoryIdempotencyStore(),
        publisher=ServiceBusPublisher(sender),
        receiver=receiver,
    )


def _envelope(blob_url: str, **over) -> dict[str, Any]:
    base = {
        "messageId": "m-1",
        "schemaVersion": "1.0",
        "documentId": "doc-1",
        "documentGuid": "123e4567-e89b-12d3-a456-426614174000",
        "driverKey": None,
        "blobUrl": blob_url,
        "attempt": 1,
        "enqueuedAt": datetime(2026, 8, 5, tzinfo=UTC).isoformat(),
    }
    base.update(over)
    return base


# --- tests -----------------------------------------------------------------


def test_end_to_end_consume_persist_publish():
    """GIVEN a raw-dmer message WHEN the service runs THEN it persists all
    stage artifacts, walks status to published, publishes the v2 message, and
    completes the source message."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    repo = InMemoryRepository()

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    # current_stage handed on to the next stage
    assert repo._rows["doc-1"]["current_stage"] == "NORMALIZE"

    # pipeline_status walked the extraction slice to EXTRACTED
    assert repo.transitions == [
        PipelineStatus.RECEIVED,
        PipelineStatus.DOWNLOADED,
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
    ]

    # all extraction artifacts persisted under the single extracted-dmer
    # container (the seeded source PDF lives in the separate `raw` input container)
    output_containers = {c for (c, _p) in blob_service.store if c != "raw"}
    assert output_containers == {"extracted-dmer"}
    assert "combined-extracted-dmer" not in output_containers
    paths = {p for (_c, p) in blob_service.store}
    assert any(p.endswith("top_level.json") for p in paths)
    assert any(p.endswith("ocr.json") for p in paths)
    assert any(p.endswith("handwritten.json") for p in paths)
    assert any(p.endswith("combined.json") for p in paths)

    # published exactly one dmer-extracted message pointing at the combined result
    assert len(sender.sent) == 1
    body = json.loads(_sb_body(sender.sent[0]))
    assert body["documentId"] == "doc-1"
    assert body["documentGuid"] == "123e4567-e89b-12d3-a456-426614174000"
    assert body["blobUrl"].endswith("combined.json")

    # source message completed (not dead-lettered)
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []


def test_failure_path_dead_letters_and_records_failed():
    """GIVEN the source document is missing WHEN the service runs THEN it
    records a failed status and the message is dead-lettered (Req 9.2)."""
    blob_service = FakeBlobService()  # nothing seeded -> download raises KeyError
    receiver = FakeSbReceiver(
        [_SbMessage(_envelope("https://acct.blob.core.windows.net/raw/missing.pdf"))]
    )
    sender = FakeSbSender()
    repo = InMemoryRepository()

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    # failure routed to MANUAL_REVIEW, nothing published, message dead-lettered
    assert PipelineStatus.MANUAL_REVIEW in repo.transitions
    assert sender.sent == []
    assert len(receiver.dead_lettered) == 1
    assert receiver.completed == []


def test_redelivery_at_extracted_republishes_without_reprocessing():
    """GIVEN a document persisted as EXTRACTED (crash before publish) WHEN
    redelivered THEN the stored pointer is re-published, extraction is not
    re-run, and the message is completed."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    repo = InMemoryRepository()
    stored = "https://acct.blob.core.windows.net/extracted-dmer/doc-1/combined.json"
    repo._rows["doc-1"] = {
        "id": "doc-1",
        "document_guid": "123e4567-e89b-12d3-a456-426614174000",
        "pipeline_status": PipelineStatus.EXTRACTED.value,
    }

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    assert repo.transitions == []  # no new transitions written
    assert {c for (c, _p) in blob_service.store} == {"raw"}  # nothing re-extracted
    assert len(sender.sent) == 1
    assert json.loads(_sb_body(sender.sent[0]))["blobUrl"] == stored
    assert len(receiver.completed) == 1


def test_redelivery_past_extraction_is_noop():
    """GIVEN downstream already advanced the document WHEN redelivered THEN it
    no-ops and the message is completed without republishing."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    repo = InMemoryRepository()
    repo._rows["doc-1"] = {
        "id": "doc-1",
        "document_guid": "123e4567-e89b-12d3-a456-426614174000",
        "pipeline_status": PipelineStatus.NORMALIZED.value,
    }

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    assert repo.transitions == []
    assert sender.sent == []
    assert len(receiver.completed) == 1


def test_extraction_starts_from_ingest_owned_row():
    """GIVEN Ingest already wrote the row at DOWNLOADED WHEN the service runs
    THEN extraction continues from there (no RECEIVED re-insert) and publishes."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    repo = InMemoryRepository()
    repo._rows["doc-1"] = {
        "id": "doc-1",
        "document_guid": "123e4567-e89b-12d3-a456-426614174000",
        "pipeline_status": PipelineStatus.DOWNLOADED.value,
    }

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    assert repo.transitions == [PipelineStatus.EXTRACTING, PipelineStatus.EXTRACTED]
    assert len(sender.sent) == 1
    assert receiver.dead_lettered == []


def test_document_uri_is_downloaded_via_storage_client():
    """GIVEN a documentUri WHEN processed THEN the storage client resolves the
    container/path from the URI (Req 3.3)."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "nested/doc-1.pdf", _make_pdf()
    )
    parsed = urlparse(doc_uri)
    assert parsed.path == "/raw/nested/doc-1.pdf"

    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    repo = InMemoryRepository()

    app = _build_app(
        receiver=receiver, blob_service=blob_service, repo=repo, sender=sender
    )
    run(app)

    assert len(sender.sent) == 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


def test_stage_run_audit_trail_end_to_end():
    """GIVEN a raw-dmer message WHEN the service runs THEN one EXTRACT stage run
    is recorded SUCCEEDED with the model version and the combined blob URL."""
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    receiver = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    sender = FakeSbSender()
    stage_runs = InMemoryStageRunRepository()

    app = _build_app(
        receiver=receiver,
        blob_service=blob_service,
        repo=InMemoryRepository(),
        sender=sender,
        stage_run_repo=stage_runs,
    )
    run(app)

    assert len(stage_runs.runs) == 1
    run_row = stage_runs.runs[0]
    assert run_row["stage"] == "EXTRACT"
    assert run_row["status"] == "SUCCEEDED"
    assert run_row["model_version"] == "di=rsbc-ocr-dmer-v9;prompt=v-test"
    assert run_row["output_blob_url"] == json.loads(_sb_body(sender.sent[0]))["blobUrl"]


def test_stage_run_failed_on_dead_letter_path():
    """GIVEN the source document is missing WHEN the service runs THEN the stage
    run is recorded FAILED with the error type."""
    receiver = FakeSbReceiver(
        [_SbMessage(_envelope("https://acct.blob.core.windows.net/raw/missing.pdf"))]
    )
    stage_runs = InMemoryStageRunRepository()
    app = _build_app(
        receiver=receiver,
        blob_service=FakeBlobService(),
        repo=InMemoryRepository(),
        sender=FakeSbSender(),
        stage_run_repo=stage_runs,
    )
    run(app)

    assert [r["status"] for r in stage_runs.runs] == ["FAILED"]
    assert stage_runs.runs[0]["error_code"] == "SOURCE_DOWNLOAD_FAILED"
    assert stage_runs.runs[0]["error_detail"] == "error=KeyError"
    # the same code is the Service Bus dead-letter reason
    assert [reason for _msg, reason in receiver.dead_lettered] == [
        "SOURCE_DOWNLOAD_FAILED"
    ]


def test_duplicate_delivery_across_service_instances_runs_once():
    """GIVEN two separately built service instances (e.g. two replicas) sharing a
    durable idempotency store WHEN the same dmer-raw message reaches both THEN
    the pipeline runs once and the second delivery is completed as a duplicate."""
    store = InMemoryIdempotencyStore()  # stands in for the shared PostgreSQL table
    blob_service = FakeBlobService()
    doc_uri = blob_service.seed(
        "https://acct.blob.core.windows.net", "raw", "doc-1.pdf", _make_pdf()
    )
    sender = FakeSbSender()
    first_rx = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])
    second_rx = FakeSbReceiver([_SbMessage(_envelope(doc_uri))])

    for rx in (first_rx, second_rx):
        run(
            _build_app(
                receiver=rx,
                blob_service=blob_service,
                repo=InMemoryRepository(),
                sender=sender,
                idempotency_store=store,
            )
        )

    assert len(sender.sent) == 1  # published once
    assert len(first_rx.completed) == 1
    assert len(second_rx.completed) == 1  # duplicate settled, not reprocessed
