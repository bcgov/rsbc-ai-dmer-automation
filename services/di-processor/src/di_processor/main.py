"""di-processor entrypoint: start the health server, then run the consumer loop.

Wiring lives here (the composition root): load typed settings, construct the
shared ``dmer_common`` clients (Blob, DI, OpenAI, PostgreSQL, Service Bus), build
the :class:`Pipeline`, and drive the ``dmer-raw`` receive loop. All I/O is
delegated to ``dmer_common`` — no raw SDK client is opened here beyond the
Service Bus receiver/sender the shared consumer/publisher wrap.

Readiness (``/readyz``) reports ``True`` only after wiring completes so the
Container App does not receive traffic before it can process it (Requirement 1.4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from dmer_common.db import (
    DmerDocumentRepository,
    DmerStageRunRepository,
    ExtractionRepository,
)
from dmer_common.doc_intelligence import DocumentIntelligenceClient
from dmer_common.messaging import (
    IdempotencyStore,
    PostgresIdempotencyStore,
    ServiceBusConsumer,
    ServiceBusPublisher,
)
from dmer_common.openai_client import OpenAIClient
from dmer_common.storage import BlobClient
from dmer_common.telemetry import get_logger

from .config import Settings, load_settings
from .consumer import idempotency_scope, make_handler
from .health import HealthServer
from .pipeline import Pipeline, PipelineConfig

_log = get_logger(__name__)


@dataclass
class Application:
    """Fully wired service components (composition root output)."""

    settings: Settings
    pipeline: Pipeline
    consumer: ServiceBusConsumer
    receiver: Any
    handler: Any


def build_application(
    settings: Settings,
    *,
    blob: BlobClient,
    di_custom: DocumentIntelligenceClient,
    di_ocr: DocumentIntelligenceClient,
    openai: OpenAIClient,
    repository: DmerDocumentRepository,
    extraction_repository: ExtractionRepository,
    stage_run_repository: DmerStageRunRepository,
    publisher: ServiceBusPublisher,
    receiver: Any,
    idempotency_store: IdempotencyStore,
) -> Application:
    """Assemble the pipeline and consumer from pre-built clients.

    Kept separate from :func:`main` so tests can inject fakes without touching
    Azure. The receiver is the Service Bus receiver for ``dmer-raw``.
    """
    pipeline = Pipeline(
        config=PipelineConfig(
            custom_model_id=settings.custom_model_id,
            prompt_version=settings.prompt_version,
        ),
        blob=blob,
        di_custom=di_custom,
        di_ocr_client=di_ocr,
        openai=openai,
        repository=repository,
        extraction_repository=extraction_repository,
        stage_run_repository=stage_run_repository,
        publisher=publisher,
    )
    consumer = ServiceBusConsumer(
        receiver,
        idempotency_scope=idempotency_scope(settings.dmer_raw_queue),
        idempotency_store=idempotency_store,
    )
    return Application(
        settings=settings,
        pipeline=pipeline,
        consumer=consumer,
        receiver=receiver,
        handler=make_handler(pipeline),
    )


def run(app: Application) -> None:
    """Serve health endpoints and process ``dmer-raw`` until interrupted.

    Blocks on the Service Bus receiver, handing each message to the shared
    consumer (which parses the envelope, enforces idempotency, and settles the
    message). The health server runs on a background thread.
    """
    ready = {"value": True}
    with HealthServer(app.settings.health_port, is_ready=lambda: ready["value"]):
        _log.info(
            "di-processor started; consuming",
            extra={"queue": app.settings.dmer_raw_queue},
        )
        try:
            for message in app.receiver:
                try:
                    app.consumer.handle(message, app.handler)
                except Exception as exc:  # noqa: BLE001 - keep the loop alive
                    # The consumer has already logged the error and dead-lettered
                    # the message; swallow here so one bad message does not stop
                    # the service consuming the rest of the queue.
                    _log.debug(
                        "message handling raised; continuing",
                        extra={"error": type(exc).__name__},
                    )
        except KeyboardInterrupt:  # pragma: no cover - graceful shutdown
            _log.info("shutdown requested")
        finally:
            ready["value"] = False


def main() -> None:  # pragma: no cover - thin production wiring
    """Production entrypoint: build clients from config, then run.

    Constructs the shared clients using Managed Identity (Blob, DI, PostgreSQL,
    Service Bus) and the Key Vault OpenAI key, then delegates to :func:`run`.
    """
    from azure.identity import DefaultAzureCredential
    from azure.servicebus import ServiceBusClient
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    settings = load_settings()
    credential = DefaultAzureCredential()

    sb_client = ServiceBusClient(settings.service_bus_namespace_fqdn, credential)
    receiver = sb_client.get_queue_receiver(settings.dmer_raw_queue)
    sender = sb_client.get_queue_sender(settings.dmer_extracted_queue)

    # NullPool: each message runs in its own event loop (asyncio.run), and pooled
    # asyncpg connections are bound to the loop that created them — reusing one
    # from the next message fails. A fresh connection per operation avoids that
    # (and suits per-connection Managed Identity tokens, still to be added).
    engine = create_async_engine(
        f"postgresql+asyncpg://{settings.postgres_host}/postgres", poolclass=NullPool
    )

    app = build_application(
        settings,
        blob=BlobClient(settings.blob_account_url, credential=credential),
        di_custom=DocumentIntelligenceClient(
            settings.doc_intelligence_endpoint, credential=credential
        ),
        di_ocr=DocumentIntelligenceClient(
            settings.doc_intelligence_endpoint, credential=credential
        ),
        openai=OpenAIClient(),
        repository=DmerDocumentRepository(engine),
        extraction_repository=ExtractionRepository(engine),
        stage_run_repository=DmerStageRunRepository(engine),
        publisher=ServiceBusPublisher(sender),
        receiver=receiver,
        idempotency_store=PostgresIdempotencyStore(engine),
    )
    run(app)


if __name__ == "__main__":  # pragma: no cover
    main()
