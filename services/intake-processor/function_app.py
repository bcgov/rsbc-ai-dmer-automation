# intake-processor — Azure Functions (Python v2 model)
#
# Ingest stage of the revised architecture (docs/development/stages/01-ingest.md),
# replacing the original single-function `dmer_intake` design. Three
# components, per that doc:
#
# - Page Poller (timer): calls Mercury's batch GET for one page, upserts
#   `dmer_document`/`driver`, publishes one `dmer-ingest` message per
#   document.
# - Ingest Function (Service Bus trigger on dmer-ingest): downloads the
#   source PDF (from `dmer_document.document_url`, not the queue message --
#   see that column's comment in database/migrations/V0001__*.sql for why),
#   writes it to `raw-dmer`, publishes to `dmer-raw`.
# - Webhook Listener (HTTP, scaffolded but inactive): Mercury doesn't emit
#   new-DMER events yet (question M-8, confirmed) -- this has no live data
#   source, so it's registered but returns 501 rather than being wired to
#   anything.
#
# Deliberately NOT built here, per an explicit decision this session: the
# architecture doc's "poller publishes a message to itself to fetch the next
# page immediately" self-continuation, since no such queue is defined
# anywhere in docs/development/message-contracts.md. One page per timer
# tick instead -- see 01-ingest.md's step 7 for the full reasoning and the
# other two options considered.
#
# Async throughout for the Postgres layer (dmer_common.db's repositories are
# async-only), but Mercury/Blob/Service Bus calls stay on their existing
# synchronous SDK clients (matching the original design, already proven this
# session) -- each such call is offloaded to a thread via `_run_sync` so it
# doesn't block the event loop other concurrent invocations share.
#
# See docs/services/intake-processor.md for background on the *original*
# architecture this superseded, and docs/development/stages/01-ingest.md for
# the current one.

from __future__ import annotations

import asyncio
import functools
import json
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from dmer_common.db import (
    DmerDocumentRepository,
    DmerStageRunRepository,
    DriverRepository,
    PollCheckpointRepository,
)
from dmer_common.mercury_client import MercuryClient
from dmer_common.telemetry import document_id_context, get_logger

app = func.FunctionApp()

_log = get_logger(__name__)

# Token scope for AAD/Managed Identity auth against Azure Database for
# PostgreSQL Flexible Server -- same as the original design.
_AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# Same fixed advisory-lock key the original single-function design used for
# "only one poll runs at a time" -- see 01-ingest.md's Page Poller
# "Concurrency" section for why a lock is still the chosen mechanism here.
# Held on one dedicated connection for the whole poll, released (explicitly,
# then for certain by closing the connection) at the end -- not the same
# connection the repositories use for their own individual operations.
_MERCURY_POLL_LOCK_KEY = 727_100_001

# Single poll source for now -- "BACKLOG" per data-model.md's poll_checkpoint
# table ("per source (BACKLOG or REALTIME)"). REALTIME would be the Webhook
# Listener's, once Mercury actually emits events to listen for (question M-8).
_POLL_SOURCE = "BACKLOG"


async def _run_sync(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in a thread so it doesn't stall the event loop
    other concurrent invocations of this app share -- see module docstring.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def _get_async_engine() -> AsyncEngine:
    """Build a fresh async Postgres engine for this invocation.

    Mirrors the original design's `_get_postgres_connection` (POSTGRES_PASSWORD
    when set, for local dev; otherwise a fresh Managed Identity token as the
    password, per Azure Database for PostgreSQL's AAD auth flow) but returns
    an async SQLAlchemy engine instead of a psycopg2 connection, since
    dmer_common.db's repositories are async-only. NullPool: no connection
    pooling across invocations -- each invocation gets its own engine,
    disposed at the end, matching the original's "open/close per call" style
    rather than introducing pool lifecycle management this rewrite doesn't
    otherwise need.
    """
    host = os.environ["POSTGRES_HOST"]
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    database = os.environ.get("POSTGRES_DATABASE", "dmer")
    user = os.environ["POSTGRES_USER"]
    password = os.environ.get("POSTGRES_PASSWORD")
    sslmode = os.environ.get("POSTGRES_SSLMODE", "require")

    if not password:
        credential = DefaultAzureCredential()
        try:
            password = await _run_sync(credential.get_token, _AAD_POSTGRES_SCOPE)
            password = password.token
        finally:
            await _run_sync(credential.close)

    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    connect_args = {} if sslmode == "disable" else {"ssl": sslmode}
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)


def _parse_mercury_datetime(value: str | None) -> datetime | None:
    """Mercury's ISO-8601 timestamps (e.g. "2024-10-03T07:11:00Z") -- empty
    string and None both mean "not present", matching how Mercury represents
    an unset date (see e.g. dps_date, "empty = not yet triaged", question I-9).
    """
    if not value:
        return None
    return datetime.fromisoformat(value)


def _mercury_auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['MERCURY_API_KEY']}"}


def _download_source_pdf(document_url: str) -> bytes:
    """Blocking download of the source PDF from Mercury's pre-signed URL."""
    request = urllib.request.Request(document_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return response.read()
    except urllib.error.HTTPError:
        raise


def _upload_raw_dmer_blob(content: bytes, document_guid: str, container_name: str) -> str:
    """Uploads to the deterministic path raw-dmer/{yyyy}/{MM}/{document_guid}.pdf
    (see 01-ingest.md) -- a retry overwrites the same blob rather than
    creating a second copy. Returns the blob's full HTTPS URL.

    Connection-string auth (AzureWebJobsStorage), not Managed Identity,
    matching this Function App's existing deployment-storage link -- see
    modules/compute/function-app.bicep's header for why that's deliberate,
    not yet switched over. dmer_common.storage.BlobClient is Managed-Identity
    -only, so it doesn't fit here without changing that decision too --
    not done in this pass.
    """
    now = datetime.now(UTC)
    blob_name = f"{now:%Y}/{now:%m}/{document_guid}.pdf"
    blob_service = BlobServiceClient.from_connection_string(
        os.environ["AzureWebJobsStorage"]
    )
    blob_client = blob_service.get_container_client(container_name).get_blob_client(
        blob_name
    )
    blob_client.upload_blob(content, overwrite=True)
    return blob_client.url


def _publish_message(queue_name: str, message_id: str, body: dict) -> None:
    """Publishes one envelope to `queue_name`, with the Service Bus
    `MessageId` set to `message_id` so the queue's own duplicate-detection
    window is the idempotency mechanism (see 01-ingest.md/message-contracts.md).

    camelCase on the wire, matching dmer_common.dto.Envelope's convention --
    message-contracts.md's own illustrative JSON example is snake_case, but
    that's the doc being informal, not a different wire contract; the actual
    working messaging code (dmer_common.messaging's publisher/consumer) is
    camelCase-only, and this repo's envelope convention is treated as
    system-wide, not per-queue.
    """
    fully_qualified_namespace = os.environ["SERVICE_BUS_NAMESPACE_FQDN"]
    with ServiceBusClient(
        fully_qualified_namespace, DefaultAzureCredential()
    ) as client, client.get_queue_sender(queue_name) as sender:
        sender.send_messages(
            ServiceBusMessage(json.dumps(body), message_id=message_id)
        )


def _envelope(
    *,
    document_id: str,
    document_guid: str,
    driver_key: str | None,
    blob_url: str | None,
) -> dict:
    """The one shared envelope shape (message-contracts.md) -- `blob_url` is
    left null on a dmer-ingest message (nothing to point at yet; Ingest
    reads document_url from dmer_document instead, see that column's
    comment) and populated on dmer-raw once the download exists.
    """
    return {
        "schemaVersion": "1.0",
        "documentId": document_id,
        "documentGuid": document_guid,
        "driverKey": driver_key,
        "blobUrl": blob_url,
        "attempt": 1,
        "enqueuedAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }


# ---------------------------------------------------------------------------
# Page Poller
# ---------------------------------------------------------------------------


async def _upsert_document_and_driver(
    record: dict,
    *,
    doc_repo: DmerDocumentRepository,
    driver_repo: DriverRepository,
) -> tuple[str, str | None]:
    """Steps 3-5 of the Page Poller (01-ingest.md) for one Mercury record.
    Returns (document_id, driver_key).
    """
    document_guid = record["document_guid"]

    driver_key = None
    driver_obj = record.get("driver")
    if driver_obj and driver_obj.get("licence_number"):
        driver_key = await driver_repo.upsert(
            driver_obj["licence_number"],
            mercury_driver_id=driver_obj.get("driver_id"),
            first_name=driver_obj.get("first_name"),
            last_name=driver_obj.get("last_name"),
            last_synced_at=datetime.now(UTC),
        )

    case_obj = record.get("case")
    mercury_case_id = case_obj.get("case_id") if case_obj else None

    document_id = await doc_repo.upsert_received(
        document_guid=document_guid,
        document_name=record.get("document_name"),
        mercury_document_status=record.get("document_status"),
        document_priority=record.get("document_priority"),
        received_date=_parse_mercury_datetime(record.get("received_date")),
        # Not present at the top level of Mercury's response shape today
        # (only nested per-document under driver.documents[].dps_date, which
        # doesn't map 1:1 to this record) -- left null ("not yet triaged")
        # until Mercury's real shape or question I-9's answer clarifies this.
        dps_date=None,
        # dps_queue ("General"/"Unknown") is the DPS General/Unknown concept
        # data-model.md's `queue` column notes describe; Mercury's `queue`
        # field ("Team - Intake") is a different, human-facing work-queue
        # label -- prefer dps_queue, fall back to queue if it's ever absent.
        queue=record.get("dps_queue") or record.get("queue"),
        business_area=record.get("document_type_business_area"),
        mercury_case_id=mercury_case_id,
        driver_key=driver_key,
        document_url=record.get("document_url"),
        now=datetime.now(UTC),
    )
    return document_id, driver_key


async def _run_poll(engine: AsyncEngine) -> None:
    checkpoint_repo = PollCheckpointRepository(engine)
    doc_repo = DmerDocumentRepository(engine)
    driver_repo = DriverRepository(engine)

    checkpoint = await checkpoint_repo.get(_POLL_SOURCE)
    last_cursor = checkpoint.last_cursor if checkpoint else None

    mercury = MercuryClient()
    queue = os.environ.get("MERCURY_QUEUE", "BOTH")
    page_size = int(os.environ.get("MERCURY_PAGE_SIZE", "50"))
    page = await _run_sync(
        functools.partial(
            mercury.get_page, queue=queue, page_size=page_size, next_url=last_cursor
        )
    )

    for record in page.records:
        document_id, driver_key = await _upsert_document_and_driver(
            record, doc_repo=doc_repo, driver_repo=driver_repo
        )
        with document_id_context(document_id):
            # Published unconditionally for every record on the page --
            # including ones already known -- relying on Service Bus
            # duplicate detection (MessageId=document_guid) and the Ingest
            # Function's own replay guard as the idempotency layers, per
            # 01-ingest.md's "Idempotency requirements". No need to track
            # "was this actually new" for correctness, only for the log line
            # below.
            await _run_sync(
                _publish_message,
                os.environ.get("DMER_INGEST_QUEUE", "dmer-ingest"),
                record["document_guid"],
                _envelope(
                    document_id=document_id,
                    document_guid=record["document_guid"],
                    driver_key=driver_key,
                    blob_url=None,
                ),
            )
            _log.info(
                "published dmer-ingest message",
                extra={"document_guid": record["document_guid"]},
            )

    now = datetime.now(UTC)
    await checkpoint_repo.update(
        _POLL_SOURCE, last_cursor=page.next_url, last_received_date=now, last_run_at=now
    )
    _log.info(
        "poll complete",
        extra={
            "record_count": len(page.records),
            "has_next": bool(page.next_url),
        },
    )


@app.timer_trigger(
    schedule="%DMER_POLL_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
async def dmer_poll(timer: func.TimerRequest) -> None:
    """Page Poller -- one page per tick, no self-continuation (see module
    docstring and 01-ingest.md's step 7 for why). Singleton via a Postgres
    advisory lock, same mechanism and key the original design used.
    """
    if timer.past_due:
        _log.warning("dmer_poll: this poll is running late (past_due)")
    _log.info("dmer_poll: triggered")

    engine = await _get_async_engine()
    try:
        async with engine.connect() as lock_conn:
            acquired = (
                await lock_conn.execute(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": _MERCURY_POLL_LOCK_KEY},
                )
            ).scalar_one()
            if not acquired:
                _log.info(
                    "dmer_poll: advisory lock already held by another invocation; "
                    "skipping this tick"
                )
                return
            try:
                await _run_poll(engine)
            except Exception:
                _log.exception("dmer_poll: poll failed")
                raise
            finally:
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": _MERCURY_POLL_LOCK_KEY},
                )
    finally:
        await engine.dispose()
        _log.info("dmer_poll: finished")


# ---------------------------------------------------------------------------
# Ingest Function
# ---------------------------------------------------------------------------


@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%DMER_INGEST_QUEUE%",
    connection="ServiceBusConnection",
)
async def dmer_ingest(msg: func.ServiceBusMessage) -> None:
    """Downloads one document and hands it to Extraction (see 01-ingest.md).

    Completion/dead-lettering is the Functions host's own default behaviour
    for this trigger type: returning normally completes the message;
    raising leaves it for redelivery, up to dmer-ingest's maxDeliveryCount
    (5) before the *queue* dead-letters it automatically. Explicit
    immediate dead-lettering for a known-poison message (message-contracts.md's
    stated preference over waiting out the delivery count) isn't implemented
    in this pass -- a documented gap, not an oversight.
    """
    envelope = json.loads(msg.get_body().decode("utf-8"))
    document_id = envelope["documentId"]
    document_guid = envelope["documentGuid"]
    driver_key = envelope.get("driverKey")

    with document_id_context(document_id):
        _log.info("dmer_ingest: triggered", extra={"document_guid": document_guid})

        engine = await _get_async_engine()
        try:
            doc_repo = DmerDocumentRepository(engine)
            stage_run_repo = DmerStageRunRepository(engine)

            row = await doc_repo.get_by_id(document_id)
            if row is None:
                _log.error("dmer_ingest: no dmer_document row for document_id")
                return
            # Replay guard -- a redelivered message must not re-download and
            # must not re-publish (01-ingest.md).
            if row.pipeline_status not in ("RECEIVED",):
                _log.info(
                    "dmer_ingest: already past DOWNLOADED (pipeline_status=%s); "
                    "no-op" % row.pipeline_status
                )
                return
            if not row.document_url:
                _log.error("dmer_ingest: dmer_document row has no document_url")
                return

            now = datetime.now(UTC)
            run_id = await stage_run_repo.start(
                document_id=document_id, stage="INGEST", attempt_no=row.attempt_count + 1,
                started_at=now,
            )

            try:
                content = await _run_sync(_download_source_pdf, row.document_url)
                raw_blob_url = await _run_sync(
                    _upload_raw_dmer_blob,
                    content,
                    document_guid,
                    os.environ.get("DMER_RAW_CONTAINER", "raw-dmer"),
                )
                await doc_repo.mark_downloaded(
                    document_id, raw_blob_url=raw_blob_url, now=datetime.now(UTC)
                )
                await _run_sync(
                    _publish_message,
                    os.environ.get("DMER_RAW_QUEUE", "dmer-raw"),
                    document_guid,
                    _envelope(
                        document_id=document_id,
                        document_guid=document_guid,
                        driver_key=driver_key,
                        blob_url=raw_blob_url,
                    ),
                )
                await stage_run_repo.succeed(
                    run_id, ended_at=datetime.now(UTC), output_blob_url=raw_blob_url
                )
                _log.info("dmer_ingest: succeeded", extra={"raw_blob_url": raw_blob_url})
            except Exception as exc:
                await stage_run_repo.fail(
                    run_id,
                    ended_at=datetime.now(UTC),
                    error_code=type(exc).__name__,
                    error_detail=str(exc),
                )
                _log.exception("dmer_ingest: failed")
                raise
        finally:
            await engine.dispose()


# ---------------------------------------------------------------------------
# Webhook Listener -- scaffolded, inactive (question M-8: Mercury doesn't
# currently emit new-DMER events, so there is nothing live to wire this to).
# Shares the Page Poller's upsert-and-publish logic once it has a real
# payload shape to parse; not duplicated here ahead of that.
# ---------------------------------------------------------------------------


@app.route(route="mercury/webhook", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def dmer_webhook(req: func.HttpRequest) -> func.HttpResponse:
    """Scaffold only -- see module docstring. Mercury has no event source to
    call this yet, so it deliberately does nothing but report that.
    """
    _log.info("dmer_webhook: received a call, but this endpoint is not wired up yet")
    return func.HttpResponse(
        "Not implemented: Mercury does not yet emit new-DMER events "
        "(question M-8) -- this endpoint is scaffolded, not active.",
        status_code=501,
    )
