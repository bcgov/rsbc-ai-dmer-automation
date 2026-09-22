"""di-processor extraction pipeline (application orchestration).

Coordinates the extraction stages for one DMER document, delegating all I/O to
injected ``dmer_common`` clients so the orchestration is unit-testable without
Azure. The Service Bus consumer (``consumer.py``) invokes :meth:`Pipeline.run`
with the parsed ``raw-dmer-queue`` envelope and handles settlement:
handler success -> complete; handler raises -> dead-letter.

Stages (page 1 only):
  A. custom-model top-level extraction  -> extracted-dmer/<doc>/top_level.json
  B. render + tile + prebuilt-read OCR  -> extracted-dmer/<doc>/ocr.json
  C. LLM reconstruction of handwriting  -> extracted-dmer/<doc>/handwritten.json
  D. merge (custom base + handwritten)  -> combined-extracted-dmer/<doc>/combined.json
  E. publish extracted-dmer-queue { documentId, combinedResultUri, ... }

Status transitions follow the shared state machine:
  received -> extracting (A) -> sectioning (B) -> combining (C) -> combined (D)
  -> published (E); failed on any unrecoverable error.

Idempotency: if the document is already ``published``, the run is a no-op.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from dmer_common.db import DocumentRepository, DocumentStatus
from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.dto import ExtractedDmerMessage, RawDmerMessage
from dmer_common.messaging import ServiceBusPublisher
from dmer_common.openai_client import OpenAIClient
from dmer_common.storage import (
    BlobClient,
    combined_extracted_dmer,
    combined_path,
    extracted_dmer,
    handwritten_path,
    ocr_path,
    top_level_path,
)
from dmer_common.telemetry import get_logger

from .extraction import di_ocr, llm_reconstruct, merge, render, splitter, top_level

_log = get_logger(__name__)


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
        repository: DocumentRepository,
        publisher: ServiceBusPublisher,
    ) -> None:
        self._cfg = config
        self._blob = blob
        self._di_custom = di_custom
        self._di_ocr = di_ocr_client
        self._openai = openai
        self._repo = repository
        self._publisher = publisher

    async def run(self, message: RawDmerMessage) -> None:
        """Process one ``raw-dmer-queue`` message end-to-end.

        Raises on unrecoverable failure (after recording a ``failed`` status) so
        the consumer dead-letters the message.
        """
        doc_id = message.document_id
        correlation_id = message.correlation_id

        # Idempotency: already-published documents are a no-op (Req 3.4).
        if await self._repo.get_status(doc_id) is DocumentStatus.PUBLISHED:
            _log.info(
                "document already published; skipping", extra={"document_id": doc_id}
            )
            return

        try:
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.RECEIVED
            )

            pdf_bytes = self._blob.download(message.document_uri)
            sha256 = hashlib.sha256(pdf_bytes).hexdigest()

            # Stage A: custom-model top-level extraction.
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.EXTRACTING
            )
            top = top_level.extract_top_level(
                self._di_custom, self._cfg.custom_model_id, pdf_bytes
            )
            top_uri = self._blob.upload_json(
                extracted_dmer(), top_level_path(doc_id), top.model_dump(mode="json")
            )

            # Stage B: render + tile + prebuilt-read OCR.
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.SECTIONING
            )
            image = render.render_cropped_page(pdf_bytes)
            tiles = splitter.create_tiles(image)
            ocr_result = di_ocr.ocr_tiles(
                self._di_ocr,
                tiles,
                page_number=render.PAGE_NUMBER,
                page_width=image.width,
                page_height=image.height,
            )
            self._blob.upload_json(extracted_dmer(), ocr_path(doc_id), ocr_result)

            # Stage C: LLM reconstruction of handwritten fields.
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.COMBINING
            )
            handwritten = llm_reconstruct.reconstruct(self._openai, image, ocr_result)
            self._blob.upload_json(
                extracted_dmer(),
                handwritten_path(doc_id),
                handwritten.model_dump(mode="json"),
            )

            # Stage D: merge (custom base + handwritten fill).
            combined = merge.merge(
                doc_id,
                correlation_id,
                top,
                handwritten,
                source_model_version=self._cfg.custom_model_id,
                prompt_version=self._cfg.prompt_version,
                processed_at=datetime.now(UTC),
            )
            combined_uri = self._blob.upload_json(
                combined_extracted_dmer(),
                combined_path(doc_id),
                combined.model_dump(mode="json"),
            )
            await self._repo.upsert_status(
                doc_id,
                correlation_id,
                DocumentStatus.COMBINED,
                initial_extraction_uri=top_uri,
                combined_extraction_uri=combined_uri,
            )

            # Stage E: publish downstream, then mark published.
            self._publisher.publish(
                ExtractedDmerMessage(
                    message_id=message.message_id,
                    correlation_id=correlation_id,
                    document_id=doc_id,
                    mercury_case_id=message.mercury_case_id,
                    sha256_hash=sha256,
                    combined_result_uri=combined_uri,
                    processed_at=datetime.now(UTC),
                )
            )
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.PUBLISHED
            )
            _log.info("document processed", extra={"document_id": doc_id})

        except Exception as exc:  # record failure, then let the consumer dead-letter
            _log.error(
                "pipeline failed",
                extra={"document_id": doc_id, "error": type(exc).__name__},
            )
            await self._record_failure(doc_id, correlation_id, type(exc).__name__)
            raise

    async def _record_failure(
        self, doc_id: str, correlation_id: str, reason: str
    ) -> None:
        """Best-effort ``failed`` status write (never masks the original error)."""
        try:
            await self._repo.upsert_status(
                doc_id, correlation_id, DocumentStatus.FAILED, failure_reason=reason
            )
        except Exception:  # noqa: BLE001 - don't mask the original failure
            _log.error("failed to record failure status", extra={"document_id": doc_id})
