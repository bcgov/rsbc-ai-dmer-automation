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
from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from .config import Settings, load_settings
from .consumer import idempotency_scope, make_handler
from .health import HealthServer
from .pipeline import Pipeline, PipelineConfig

_log = get_logger(__name__)

# Token scope for Microsoft Entra (Managed Identity) auth against Azure Database
# for PostgreSQL Flexible Server — the same scope Ingest uses.
AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def build_postgres_engine(settings: Settings, credential: Any) -> AsyncEngine:
    """Build the async engine for the pipeline database.

    Deployed, the password is a Microsoft Entra token for the di-processor
    Managed Identity, fetched on **every new connection** (``do_connect``)
    rather than once at startup: this is a long-running consumer and tokens
    expire, so a startup token would stop working mid-run. With ``NullPool``
    every operation opens a fresh connection; ``azure-identity`` caches the
    token until near expiry, so this does not call Entra per operation.

    ``POSTGRES_PASSWORD`` (local development only) replaces the token.

    NullPool: each message runs in its own event loop (``asyncio.run``), and
    pooled asyncpg connections are bound to the loop that created them —
    reusing one from the next message fails.
    """
    url = URL.create(
        "postgresql+asyncpg",
        username=settings.postgres_user,
        password=settings.postgres_password,
        host=settings.postgres_host,
        port=settings.postgres_port,
        database=settings.postgres_database,
    )
    connect_args = (
        {}
        if settings.postgres_sslmode == "disable"
        else {"ssl": settings.postgres_sslmode}
    )
    engine = create_async_engine(url, poolclass=NullPool, connect_args=connect_args)

    if not settings.postgres_password:

        @event.listens_for(engine.sync_engine, "do_connect")
        def _entra_token(dialect, conn_rec, cargs, cparams):
            cparams["password"] = credential.get_token(AAD_POSTGRES_SCOPE).token

    return engine


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

    settings = load_settings()
    credential = DefaultAzureCredential()

    sb_client = ServiceBusClient(settings.service_bus_namespace_fqdn, credential)
    receiver = sb_client.get_queue_receiver(settings.dmer_raw_queue)
    sender = sb_client.get_queue_sender(settings.dmer_extracted_queue)

    engine = build_postgres_engine(settings, credential)

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
