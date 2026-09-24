"""di-processor extraction pipeline (application orchestration).

Coordinates the extraction stages for one DMER document, delegating all I/O to
injected ``dmer_common`` clients so the orchestration is unit-testable without
Azure. The Service Bus consumer (``consumer.py``) invokes :meth:`Pipeline.run`
with the parsed ``dmer-raw`` envelope and handles settlement: handler success ->
complete; handler raises -> dead-letter.

Stages (page 1 only), all persisted under the single ``extracted-dmer`` container:
  A. custom-model top-level extraction  -> extracted-dmer/<doc>/top_level.json
     (+ cut-off flags from the same call's layout -> dmer_extraction)
  B. render + tile + prebuilt-read OCR  -> extracted-dmer/<doc>/ocr.json
  C. LLM reconstruction of handwriting  -> extracted-dmer/<doc>/handwritten.json
  D. merge (custom base + handwritten)  -> extracted-dmer/<doc>/combined.json
  E. publish dmer-extracted { documentId, blobUrl, ... }

Status (revised architecture, two fields on ``dmer_document`` — see
``docs/development/data-model.md``): ``current_stage`` is ``EXTRACT`` while
extracting and moves to ``NORMALIZE`` with ``EXTRACTED`` (the next stage owns it);
a ``MANUAL_REVIEW`` write leaves ``current_stage`` where it was.
Ingest owns ``RECEIVED`` and ``DOWNLOADED`` (``docs/development/stages/01-ingest.md``),
so extraction normally starts from ``DOWNLOADED``:
  DOWNLOADED -> EXTRACTING (A) -> EXTRACTED (D, persisted before E)
If Ingest has not written the row (no Ingest upstream yet) or stopped at
``RECEIVED``, di-processor bootstraps the missing ``RECEIVED`` / ``DOWNLOADED``
steps itself. On any unrecoverable error the document is routed to
``MANUAL_REVIEW`` and the error re-raised so the consumer dead-letters the message.

Concurrency: every status write is a compare-and-set against the status this
run last saw or wrote (``expected``). If another worker changed it first — e.g. a
Service Bus redelivery running alongside the original — the write raises
``StaleStatusError`` and this run **stops quietly**: no ``MANUAL_REVIEW``, no
dead-letter, message completed; the winner owns the document. The stage run is
closed ``FAILED`` / ``STALE_STATUS`` for the audit trail.

Idempotency / replay (redelivery after a crash or lock-renewal failure):
  - ``EXTRACTING``: a previous attempt died mid-run; reprocess (artifact writes
    overwrite the same blob paths).
  - ``EXTRACTED``: the combined extraction was persisted but the publish may not
    have happened; re-publish the stored pointer without re-running A-D.
  - beyond ``EXTRACTED``: downstream already consumed it; no-op.

Audit trail: every run that actually extracts writes one ``dmer_stage_run`` row
(``stage = EXTRACT``): ``RUNNING`` once the document row exists, then
``SUCCEEDED`` (with the combined blob URL) after the publish, or ``FAILED`` (with
the error type) on error. ``model_version`` records both the DI custom model and
the prompt version. Replays that only re-publish or no-op write no row.

Failures: each step runs inside ``failure_step(<FailureCode>)``, so a failure
is reported by *which step* failed. The code and a PII-safe detail (exception
type, HTTP status, circuit state — never the message) go to
``dmer_stage_run.error_code`` / ``error_detail``, the error log, and — via
:class:`~di_processor.failures.PipelineFailure` — the dead-letter reason. See
:mod:`di_processor.failures`.

Out of scope: driver resolution and ``driver_evaluation`` counting (decided not
to be done in Extraction — the ``driver_key`` is forwarded as received, and only
the page's licence is recorded), and DLQ failure classification (ADR-0002).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from dmer_common.db import (
    DmerDocumentRepository,
    DmerStageRunRepository,
    ExtractionRecord,
    ExtractionRepository,
    PipelineStage,
    PipelineStatus,
    StaleStatusError,
)
from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.dto import (
    EXTRACTED_EVENT,
    ExtractedMessage,
    RawMessage,
    event_message_id,
)
from dmer_common.messaging import ServiceBusPublisher
from dmer_common.openai_client import OpenAIClient
from dmer_common.storage import (
    BlobClient,
    combined_path,
    extracted_dmer,
    handwritten_path,
    ocr_path,
    top_level_path,
)
from dmer_common.telemetry import get_logger

from .extraction import (
    di_ocr,
    licence,
    llm_reconstruct,
    merge,
    render,
    splitter,
    top_level,
)
from .failures import FailureCode, PipelineFailure, as_failure, failure_step

_log = get_logger(__name__)

# Statuses past extraction: downstream has taken the document over, so a
# redelivered dmer-raw message is a no-op. (EXTRACTED itself is handled by
# re-publishing, since the publish may not have happened.)
_PAST_EXTRACTION: frozenset[PipelineStatus] = frozenset(
    {
        PipelineStatus.NORMALIZED,
        PipelineStatus.RULES_APPLIED,
        PipelineStatus.AWAITING_DRIVER_COMPLETION,
        PipelineStatus.DECIDED,
        PipelineStatus.POSTING,
        PipelineStatus.COMPLETED,
    }
)


@dataclass(frozen=True)
class PipelineConfig:
    """Runtime configuration for the pipeline."""

    custom_model_id: str
    prompt_version: str | None = None


class Pipeline:
    """Orchestrates Stages A-E for a single document.

    All external systems are injected as ``dmer_common`` clients so the
    orchestration can be unit-tested with fakes/mocks.
    """

    def __init__(
        self,
        *,
        config: PipelineConfig,
        blob: BlobClient,
        di_custom: DocumentIntelligenceClient,
        di_ocr_client: DocumentIntelligenceClient,
        openai: OpenAIClient,
        repository: DmerDocumentRepository,
        extraction_repository: ExtractionRepository,
        stage_run_repository: DmerStageRunRepository,
        publisher: ServiceBusPublisher,
    ) -> None:
        self._cfg = config
        self._blob = blob
        self._di_custom = di_custom
        self._di_ocr = di_ocr_client
        self._openai = openai
        self._repo = repository
        self._extractions = extraction_repository
        self._stage_runs = stage_run_repository
        self._publisher = publisher

    async def run(self, message: RawMessage) -> None:
        """Process one ``dmer-raw`` message end-to-end.

        Raises on unrecoverable failure (after routing to ``MANUAL_REVIEW``) so
        the consumer dead-letters the message.
        """
        doc_id = message.document_id

        with failure_step(FailureCode.DB_READ_FAILED):
            current = await self._repo.get_status(doc_id)
        if current in _PAST_EXTRACTION:
            _log.info(
                "document already past extraction; skipping",
                extra={"document_id": doc_id},
            )
            return

        if current is PipelineStatus.EXTRACTED:
            # Re-publish only — no extraction runs, so no stage-run row.
            try:
                await self._republish(message)
            except Exception as exc:
                failure = as_failure(exc, FailureCode.UNEXPECTED)
                self._log_failure("re-publish failed", doc_id, failure)
                if not await self._route_to_manual_review(
                    doc_id, expected=PipelineStatus.EXTRACTED
                ):
                    return  # another worker moved the document on
                if failure is exc:
                    raise
                raise failure from exc
            return

        run_id: int | None = None
        # The status this run last saw or wrote: the ``expected`` value for its
        # next compare-and-set write.
        status = current
        try:
            if status is None:
                # No Ingest-written row: bootstrap it (requires document_guid).
                with failure_step(FailureCode.DB_WRITE_FAILED):
                    await self._repo.upsert_status(
                        doc_id,
                        PipelineStatus.RECEIVED,
                        expected=None,
                        document_guid=message.document_guid,
                        stage=PipelineStage.EXTRACT,
                    )
                status = PipelineStatus.RECEIVED

            # Audit trail: open this attempt's stage run (needs the document row).
            with failure_step(FailureCode.DB_WRITE_FAILED):
                run_id = await self._stage_runs.start(
                    document_id=doc_id,
                    stage=PipelineStage.EXTRACT,
                    model_version=self._model_version(),
                )

            with failure_step(FailureCode.SOURCE_DOWNLOAD_FAILED):
                pdf_bytes = self._blob.download(message.blob_url)
            _ = hashlib.sha256(pdf_bytes).hexdigest()  # source-doc hash (reserved)

            with failure_step(FailureCode.DB_WRITE_FAILED):
                if status is PipelineStatus.RECEIVED:
                    await self._repo.upsert_status(
                        doc_id,
                        PipelineStatus.DOWNLOADED,
                        expected=status,
                        stage=PipelineStage.EXTRACT,
                    )
                    status = PipelineStatus.DOWNLOADED
                # From DOWNLOADED this advances; from EXTRACTING (mid-run
                # redelivery) it is an idempotent re-entry.
                await self._repo.upsert_status(
                    doc_id,
                    PipelineStatus.EXTRACTING,
                    expected=status,
                    stage=PipelineStage.EXTRACT,
                )
                status = PipelineStatus.EXTRACTING

            # Stage A: custom-model top-level extraction (+ cut-off flags).
            with failure_step(FailureCode.DI_CUSTOM_MODEL_FAILED):
                top = top_level.extract_top_level(
                    self._di_custom, self._cfg.custom_model_id, pdf_bytes
                )
            with failure_step(FailureCode.ARTIFACT_WRITE_FAILED):
                self._blob.upload_json(
                    extracted_dmer(),
                    top_level_path(doc_id),
                    top.model_dump(mode="json"),
                )

            # Stage B: render + tile + prebuilt-read OCR.
            with failure_step(FailureCode.PDF_UNREADABLE):
                image = render.render_cropped_page(pdf_bytes)
            tiles = splitter.create_tiles(image)
            with failure_step(FailureCode.OCR_FAILED):
                ocr_result = di_ocr.ocr_tiles(
                    self._di_ocr,
                    tiles,
                    page_number=render.PAGE_NUMBER,
                    page_width=image.width,
                    page_height=image.height,
                )
            with failure_step(FailureCode.ARTIFACT_WRITE_FAILED):
                self._blob.upload_json(extracted_dmer(), ocr_path(doc_id), ocr_result)

            # Stage C: LLM reconstruction of handwritten fields (classifies its
            # own call vs output failures).
            handwritten = llm_reconstruct.reconstruct(self._openai, image, ocr_result)
            with failure_step(FailureCode.ARTIFACT_WRITE_FAILED):
                self._blob.upload_json(
                    extracted_dmer(),
                    handwritten_path(doc_id),
                    handwritten.model_dump(mode="json"),
                )

            # Stage D: merge (custom base + handwritten fill) -> combined.json.
            combined = merge.merge(
                doc_id,
                top,
                handwritten,
                source_model_version=self._cfg.custom_model_id,
                prompt_version=self._cfg.prompt_version,
                processed_at=datetime.now(UTC),
            )
            with failure_step(FailureCode.ARTIFACT_WRITE_FAILED):
                combined_uri = self._blob.upload_json(
                    extracted_dmer(),
                    combined_path(doc_id),
                    combined.model_dump(mode="json"),
                )
            # Queryable extraction values (licence read, cut-off flags) for driver
            # resolution and the Decision Gateway. Upsert on document_id, so a
            # reprocessed run overwrites. The licence is PII: log presence only.
            licence_read = licence.read_licence(top)
            _log.info(
                "licence read from page",
                extra={"document_id": doc_id, "licence_read": licence_read is not None},
            )
            with failure_step(FailureCode.DB_WRITE_FAILED):
                await self._extractions.upsert(
                    ExtractionRecord(
                        document_id=doc_id,
                        licence_number_read=licence_read,
                        has_header=combined.cutoff.has_header,
                        has_signature=combined.cutoff.has_signature,
                        is_cutoff=combined.cutoff.is_cutoff,
                    )
                )
                # Persist EXTRACTED + the pointer *before* publishing, so a crash
                # between the two is recovered by the EXTRACTED replay branch
                # above. current_stage -> NORMALIZE: extraction is done, the
                # document now waits on the next stage.
                await self._repo.upsert_status(
                    doc_id,
                    PipelineStatus.EXTRACTED,
                    expected=status,
                    stage=PipelineStage.NORMALIZE,
                )
                status = PipelineStatus.EXTRACTED

            # Stage E: publish downstream (pointer to the combined extraction).
            with failure_step(FailureCode.PUBLISH_FAILED):
                self._publish(message, combined_uri)
            _log.info("document processed", extra={"document_id": doc_id})

        except Exception as exc:  # route to MANUAL_REVIEW, then let consumer DLQ
            # Anything not raised inside a failure_step (merge, tiling, ...) is a
            # bug: UNEXPECTED.
            failure = as_failure(exc, FailureCode.UNEXPECTED)
            await self._fail_stage_run(run_id, doc_id, failure)
            if failure.code is FailureCode.STALE_STATUS:
                # Lost a compare-and-set race: another worker owns the document.
                # Stop without MANUAL_REVIEW or dead-lettering (the message is
                # completed); the winner's result stands.
                _log.warning(
                    "status changed by another worker; stopping",
                    extra={
                        "document_id": doc_id,
                        "error_code": failure.code.value,
                        "error_detail": failure.safe_detail,
                    },
                )
                return
            self._log_failure("pipeline failed", doc_id, failure)
            if not await self._route_to_manual_review(doc_id, expected=status):
                # Another worker moved the document on meanwhile: its result
                # stands, so don't dead-letter this (duplicate) delivery.
                return
            if failure is exc:
                raise
            raise failure from exc
        else:
            # Outside the try body: the document is already published and
            # downstream owns it, so an audit-write failure here must not route
            # it to MANUAL_REVIEW.
            await self._succeed_stage_run(run_id, doc_id, combined_uri)

    @staticmethod
    def _log_failure(event: str, doc_id: str, failure: PipelineFailure) -> None:
        """Log a classified failure: code + safe detail only, never the message."""
        _log.error(
            event,
            extra={
                "document_id": doc_id,
                "error_code": failure.code.value,
                "error_detail": failure.safe_detail,
            },
        )

    async def _succeed_stage_run(
        self, run_id: int | None, doc_id: str, blob_url: str
    ) -> None:
        """Best-effort ``SUCCEEDED`` stage-run write after a successful publish."""
        if run_id is None:
            return
        try:
            await self._stage_runs.succeed(run_id, output_blob_url=blob_url)
        except Exception:  # noqa: BLE001 - the document itself succeeded
            _log.error(
                "failed to record SUCCEEDED stage run", extra={"document_id": doc_id}
            )

    def _model_version(self) -> str:
        """``di=<custom model>;prompt=<prompt version>`` for ``dmer_stage_run``."""
        prompt = self._cfg.prompt_version or "unversioned"
        return f"di={self._cfg.custom_model_id};prompt={prompt}"

    async def _fail_stage_run(
        self, run_id: int | None, doc_id: str, failure: PipelineFailure
    ) -> None:
        """Best-effort ``FAILED`` stage-run write (never masks the original error).

        Writes the failure code and its PII-safe detail — never the exception
        message, which can carry extracted values.
        """
        if run_id is None:
            return  # failed before the stage run was opened
        try:
            await self._stage_runs.fail(
                run_id,
                error_code=failure.code.value,
                error_detail=failure.safe_detail,
            )
        except Exception:  # noqa: BLE001 - don't mask the original failure
            _log.error(
                "failed to record FAILED stage run", extra={"document_id": doc_id}
            )

    async def _republish(self, message: RawMessage) -> None:
        """Re-publish the combined-extraction pointer for an EXTRACTED doc.

        Covers a crash after EXTRACTED was persisted but before (or during) the
        publish. The pointer isn't stored: ``combined.json`` always lives at
        ``extracted-dmer/<document_id>/combined.json``, so it is rebuilt from the
        path. The re-sent message carries the same deterministic ``message_id``
        as the first publish, so a consumer that already processed it no-ops.
        """
        doc_id = message.document_id
        blob_url = self._blob.blob_url(extracted_dmer(), combined_path(doc_id))
        with failure_step(FailureCode.PUBLISH_FAILED):
            self._publish(message, blob_url)
        _log.info(
            "document already extracted; re-published pointer",
            extra={"document_id": doc_id},
        )

    def _publish(self, message: RawMessage, blob_url: str) -> None:
        """Publish the ``dmer-extracted`` pointer message for ``message``.

        ``message_id`` identifies the *extraction* event, derived from the
        document — never the incoming ``dmer-raw`` ID. Every publish or replay
        for a document carries the same ID (so downstream idempotency catches
        repeats, whatever upstream message triggered it), and it can never be
        mistaken for the upstream event. ``document_id`` links the two.
        """
        self._publisher.publish(
            ExtractedMessage(
                message_id=event_message_id(EXTRACTED_EVENT, message.document_id),
                document_id=message.document_id,
                document_guid=message.document_guid,
                driver_key=message.driver_key,
                blob_url=blob_url,
                attempt=message.attempt,
                enqueued_at=datetime.now(UTC),
            )
        )

    async def _route_to_manual_review(
        self,
        doc_id: str,
        *,
        expected: PipelineStatus | None,
    ) -> bool:
        """Best-effort ``MANUAL_REVIEW`` write (never masks the original error).

        Compare-and-set against the status this run last saw: if another worker
        has moved the document on (e.g. completed it), this run's failure must
        not override that result. Returns False in that case (the run lost the
        race and should stop quietly), True otherwise.
        """
        if expected is None:
            return True  # failed before the row existed: nothing to route
        try:
            await self._repo.upsert_status(
                doc_id, PipelineStatus.MANUAL_REVIEW, expected=expected
            )
        except StaleStatusError as exc:
            _log.warning(
                "not routing to MANUAL_REVIEW: status changed by another worker",
                extra={
                    "document_id": doc_id,
                    "expected": exc.expected.value if exc.expected else None,
                    "actual": exc.actual.value if exc.actual else None,
                },
            )
            return False
        except Exception:  # noqa: BLE001 - don't mask the original failure
            _log.error(
                "failed to record MANUAL_REVIEW status",
                extra={"document_id": doc_id},
            )
        return True
