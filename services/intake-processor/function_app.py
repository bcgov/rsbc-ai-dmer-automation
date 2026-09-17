# intake-processor — Azure Functions (Python v2 model)
#
# Timer-triggered batch poller: on a schedule, calls the Mercury backlog API
# for pending DMERs, downloads each one's PDF (via the pre-signed
# `document_url` Mercury hands back) into our own blob storage, and inserts
# a "started" tracking row into `dmer_processing` for any DMER not already
# known -- for the downstream pipeline (paddle/DI processors) to pick up.
#
# Polling (rather than a blob/Event Grid trigger reacting to something
# landing in our own storage) is the actual shape of this integration: WE
# pull DMERs from Mercury and write the PDF ourselves, nothing external ever
# writes into our storage account directly. It also means the Function App
# needs no inbound access at all -- everything it does is outbound, through
# the existing VNet integration to Postgres and Storage (and, for the
# Mercury API calls themselves, straight out to the internet).
#
# Only one invocation of `dmer_intake` is ever allowed to actually do work at
# a time, even if the timer fires again before a slow poll finishes -- see
# _acquire_poll_lock. Every step of a poll is logged at INFO (the only level
# the Azure portal actually exposes) so a run can be reconstructed from the
# logs alone when debugging.
#
# See docs/services/intake-processor.md for the service's full responsibilities.

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone

import azure.functions as func
import psycopg2
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusMessage
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

logger = logging.getLogger(__name__)

# Token scope for AAD/Managed Identity auth against Azure Database for
# PostgreSQL Flexible Server.
_AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"

# Arbitrary but fixed key for the poll's Postgres advisory lock (any bigint
# works -- it's just a namespace both pg_try_advisory_lock/pg_advisory_unlock
# calls have to agree on). Session-scoped: held for the whole dmer_intake
# invocation on one dedicated connection, and released automatically by
# Postgres the moment that connection closes, crash or no crash.
_MERCURY_POLL_LOCK_KEY = 727_100_001

# Service Bus queue this publishes new-DMER notifications to -- see
# docs/contracts/queues/raw-dmer-queue.md for the full contract (envelope
# schema, retry/dedup settings). Name and envelope shape are fixed by that
# contract, not environment-configurable, so this is a constant rather than
# an app setting.
_RAW_DMER_QUEUE_NAME = "raw-dmer-queue"


def _mercury_api_get(url: str) -> dict:
    """GET a Mercury backlog API URL and return the parsed JSON body.

    Auth is a placeholder (Bearer token from MERCURY_API_KEY) until the
    real Mercury auth mechanism is confirmed -- adjust here if it turns out
    to be something else (API key header, OAuth2 client credentials, etc.).
    """
    logger.info("Calling Mercury API: GET %s", url)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {os.environ['MERCURY_API_KEY']}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode())
    logger.info(
        "Mercury API responded: %d record(s), nextLink=%s",
        len(body.get("value", [])),
        bool(body.get("nextLink")),
    )
    return body


def _get_base_mercury_url() -> str:
    """The starting (unpaginated) Mercury query -- used to seed
    `mercury_links` on the very first run, and to restart the cycle once a
    pagination pass reaches its end. Mercury's backlog keeps growing, so
    periodically re-checking from the top is the point, not a bug.

    MERCURY_QUEUE is passed straight through as the `queue` query parameter
    -- Mercury itself decides what it means (a single queue, e.g.
    "DPS_GENERAL" or "DPS_UNKNOWN", or a combined value covering both) -- see
    docs/services/intake-processor.md, "Batch Poller ... (DPS General &
    Unknown Queues)".
    """
    base_url = os.environ["MERCURY_API_BASE_URL"]
    queue = os.environ.get("MERCURY_QUEUE", "DPS_GENERAL")
    page_size = os.environ.get("MERCURY_PAGE_SIZE", "50")
    url = f"{base_url}?queue={queue}&page_size={page_size}"
    logger.info(
        "Built base Mercury URL (queue=%s, page_size=%s): %s", queue, page_size, url
    )
    return url


def _download_to_blob(
    document_url: str, dmer_id: str, container_name: str
) -> tuple[str, str]:
    """Downloads a DMER PDF from Mercury's pre-signed `document_url` and
    uploads it into our own storage, named by `dmer_id` (the Mercury
    `document_guid` -- globally unique, so this can't collide the way a
    human-entered filename could).

    Returns (blob_path, blob_url): `blob_path` as "<container>/<dmer_id>.pdf"
    for the `dmer_processing.blob_path` column, and `blob_url` -- the blob's
    full HTTPS URL -- for raw-dmer-queue's `documentUri` (see
    docs/contracts/queues/raw-dmer-queue.md). di-processor fetches from our
    own storage there, not Mercury's pre-signed URL, which may have expired
    by the time a backlogged item actually gets processed.
    """
    logger.info("Downloading dmer_id=%s from Mercury document_url", dmer_id)
    with urllib.request.urlopen(document_url, timeout=60) as response:
        content = response.read()
    logger.info("Downloaded dmer_id=%s: %d bytes", dmer_id, len(content))

    blob_service = BlobServiceClient.from_connection_string(
        os.environ["AzureWebJobsStorage"]
    )
    blob_name = f"{dmer_id}.pdf"
    blob_client = blob_service.get_container_client(container_name).get_blob_client(
        blob_name
    )
    blob_client.upload_blob(content, overwrite=True)
    blob_path = f"{container_name}/{blob_name}"
    blob_url = blob_client.url
    logger.info("Uploaded dmer_id=%s to blob_path=%s", dmer_id, blob_path)
    return blob_path, blob_url


def _publish_raw_dmer_message(
    dmer_id: str, driver_license: str | None, document_uri: str
) -> None:
    """Publishes the "a new DMER is ready" message to raw-dmer-queue for
    di-processor, per the envelope in docs/contracts/queues/raw-dmer-queue.md.

    `dmer_id` doubles as both the Service Bus message's native `message_id`
    (so the queue's own `requiresDuplicateDetection` setting -- a 10-minute
    window -- can catch an accidental resend) and the envelope's `messageId`
    field, the longer-lived, application-level idempotency key the contract
    calls for: "di-processor must no-op on a duplicate messageId it has
    already completed." Both point at the same value deliberately -- a
    DMER's own globally-unique id is a more useful idempotency key here than
    a fresh random one would be.

    `mercuryCaseId` is left null: we don't currently extract a separate
    Mercury case id from the record (see _process_mercury_records) -- only
    document_guid/document_url/driver, per the fields actually in scope.
    """
    body = {
        "messageId": dmer_id,
        "correlationId": dmer_id,
        "schemaVersion": "1.0",
        "sourceSystem": "mercury-batch",
        "documentId": dmer_id,
        "mercuryCaseId": None,
        "documentUri": document_uri,
        "receivedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {
            "driverLicense": driver_license,
        },
    }

    fully_qualified_namespace = os.environ["SERVICE_BUS_NAMESPACE_FQDN"]
    with ServiceBusClient(
        fully_qualified_namespace, DefaultAzureCredential()
    ) as client, client.get_queue_sender(_RAW_DMER_QUEUE_NAME) as sender:
        sender.send_messages(ServiceBusMessage(json.dumps(body), message_id=dmer_id))
    logger.info(
        "Published %s message for dmer_id=%s (documentUri=%s)",
        _RAW_DMER_QUEUE_NAME,
        dmer_id,
        document_uri,
    )


def _get_postgres_connection() -> psycopg2.extensions.connection:
    """Open a PostgreSQL connection.

    POSTGRES_PASSWORD is used when set, which is convenient for local
    development against a plain Postgres instance. In Azure, POSTGRES_PASSWORD
    is left unset and a Managed Identity access token is fetched instead and
    used as the password, per Azure Database for PostgreSQL's AAD auth flow.
    """
    host = os.environ["POSTGRES_HOST"]
    port = os.environ.get("POSTGRES_PORT", "5432")
    database = os.environ.get("POSTGRES_DATABASE", "dmer")
    user = os.environ["POSTGRES_USER"]
    password = os.environ.get("POSTGRES_PASSWORD")
    # Azure Database for PostgreSQL always requires SSL; a plain local Postgres
    # (e.g. for local dev/testing) typically doesn't have it configured, so
    # this is overridable via POSTGRES_SSLMODE.
    sslmode = os.environ.get("POSTGRES_SSLMODE", "require")

    if not password:
        password = DefaultAzureCredential().get_token(_AAD_POSTGRES_SCOPE).token

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        sslmode=sslmode,
    )


def _acquire_poll_lock() -> psycopg2.extensions.connection | None:
    """Tries to take the poll's Postgres advisory lock, returning the (open)
    connection holding it on success, or None if another invocation already
    holds it.

    This is `dmer_intake`'s actual concurrency guard -- a session-scoped
    advisory lock (`pg_try_advisory_lock`) held on one dedicated connection
    for the entire invocation. Unlike the previous `mercury_links.status` +
    `SELECT ... FOR UPDATE` approach, this can't be left stuck: if the
    invocation holding it crashes, is killed, or its connection otherwise
    drops, Postgres releases the lock the moment that connection closes --
    no cleanup code required, and no future invocation can ever be
    permanently blocked behind a dead one.

    `mercury_links.status` is still maintained, but purely as an audit trail
    of what happened to each link -- it no longer enforces "only one
    invocation at a time"; this lock does.
    """
    conn = _get_postgres_connection()
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (_MERCURY_POLL_LOCK_KEY,))
        acquired = cur.fetchone()[0]
    conn.commit()
    pid = conn.get_backend_pid()

    if not acquired:
        logger.info(
            "Advisory lock %s already held by another invocation (this conn pid=%s); "
            "closing and skipping this tick",
            _MERCURY_POLL_LOCK_KEY,
            pid,
        )
        conn.close()
        return None

    logger.info(
        "Acquired advisory lock %s on connection pid=%s", _MERCURY_POLL_LOCK_KEY, pid
    )
    return conn


def _release_poll_lock(conn: psycopg2.extensions.connection) -> None:
    """Releases the poll's advisory lock and closes its dedicated connection.

    Explicit `pg_advisory_unlock` first, so the lock is free immediately
    rather than only once the connection eventually gets torn down -- but
    closing the connection is what actually guarantees release. If the
    unlock call itself errors for any reason, the `finally: conn.close()`
    below still releases the lock as soon as the connection ends.
    """
    pid = conn.get_backend_pid()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MERCURY_POLL_LOCK_KEY,))
            released = cur.fetchone()[0]
        conn.commit()
        logger.info(
            "Released advisory lock %s (pid=%s, pg_advisory_unlock returned=%s)",
            _MERCURY_POLL_LOCK_KEY,
            pid,
            released,
        )
    except Exception:
        logger.exception(
            "Error explicitly releasing advisory lock %s (pid=%s); closing the "
            "connection will still release it",
            _MERCURY_POLL_LOCK_KEY,
            pid,
        )
    finally:
        conn.close()


def _get_current_link() -> tuple[int, str, str]:
    """Reads the latest (highest id) row in `mercury_links` -- the pagination
    cursor for this poll -- seeding a fresh base-query row if the table is
    empty or the latest row is 'finished' with nothing queued behind it
    (defensively; `_finish_link` always queues a fresh 'not_started' row, so
    this shouldn't normally happen).

    By the time this runs, `dmer_intake` already holds the poll's advisory
    lock, so concurrent access to this table from another invocation is not
    possible -- this is a plain read/insert, not a claim needing its own
    row-level locking.

    Returns (id, link, status) of the row to use for this poll.
    """
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, link, status FROM mercury_links ORDER BY id DESC LIMIT 1"
            )
            row = cur.fetchone()

            if row is None:
                link = _get_base_mercury_url()
                logger.info("mercury_links table is empty; seeding first link=%s", link)
                cur.execute(
                    "INSERT INTO mercury_links (link, status) "
                    "VALUES (%s, 'not_started') RETURNING id",
                    (link,),
                )
                link_id = cur.fetchone()[0]
                logger.info("Seeded mercury_links id=%s", link_id)
                return link_id, link, "not_started"

            link_id, link, status = row
            logger.info(
                "Latest mercury_links row: id=%s status=%s link=%s",
                link_id,
                status,
                link,
            )

            if status == "finished":
                link = _get_base_mercury_url()
                logger.warning(
                    "mercury_links id=%s was 'finished' with nothing queued behind it; "
                    "restarting cycle with link=%s",
                    link_id,
                    link,
                )
                cur.execute(
                    "INSERT INTO mercury_links (link, status) "
                    "VALUES (%s, 'not_started') RETURNING id",
                    (link,),
                )
                link_id = cur.fetchone()[0]
                logger.info("Seeded mercury_links id=%s", link_id)
                return link_id, link, "not_started"

            if status == "processing":
                logger.warning(
                    "mercury_links id=%s was left 'processing' -- a previous "
                    "invocation must have ended without finishing it (crash, "
                    "timeout, etc). Safe to retry now: the advisory lock "
                    "guarantees no other invocation is using it concurrently",
                    link_id,
                )

            return link_id, link, status
    finally:
        conn.close()


def _mark_link_processing(link_id: int) -> None:
    """Flips a link's status to 'processing' -- audit-trail bookkeeping only;
    actual concurrency enforcement is the advisory lock held by the caller.
    """
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mercury_links SET status = 'processing' WHERE id = %s",
                (link_id,),
            )
    finally:
        conn.close()
    logger.info("mercury_links id=%s marked processing", link_id)


def _finish_link(link_id: int, next_link: str | None) -> None:
    """Marks a claimed link 'finished' and queues the next one, ready
    ('not_started') for the next poll to use.

    Restarts from the base (unpaginated) query if Mercury returned no
    `nextLink` -- i.e. this pagination pass reached the end -- rather than
    leaving nothing queued, since Mercury's backlog keeps growing and
    periodically re-checking from the top is the intended behaviour, not an
    edge case to avoid.
    """
    link_to_queue = next_link or _get_base_mercury_url()
    logger.info(
        "Finishing mercury_links id=%s; queuing next link (restart-of-cycle=%s): %s",
        link_id,
        next_link is None,
        link_to_queue,
    )
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mercury_links SET status = 'finished' WHERE id = %s",
                (link_id,),
            )
            cur.execute(
                "INSERT INTO mercury_links (link, status) VALUES (%s, 'not_started') "
                "RETURNING id",
                (link_to_queue,),
            )
            next_id = cur.fetchone()[0]
    finally:
        conn.close()
    logger.info(
        "mercury_links id=%s marked finished; queued next id=%s", link_id, next_id
    )


def _mark_link_not_started(link_id: int) -> None:
    """Resets a link back to 'not_started' after a failure that happened
    before there was anything to show for it (the Mercury API call itself
    failing, or an unexpected error while processing its records) --
    otherwise it would sit at 'processing' until the next poll's
    `_get_current_link` warns about and retries it anyway. Resetting it here
    keeps the audit trail accurate in the meantime.
    """
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE mercury_links SET status = 'not_started' WHERE id = %s",
                (link_id,),
            )
    finally:
        conn.close()
    logger.info("mercury_links id=%s reset to not_started for retry", link_id)


def _already_known_dmer_ids(dmer_ids: list[str]) -> set[str]:
    """Which of these Mercury document_guids already have a row in
    `dmer_processing`, regardless of status.

    This is the poller's dedup mechanism: a DMER stays in Mercury's backlog
    for as long as it takes us to notice it, so without this every poll
    would re-download and re-insert everything Mercury still lists.
    """
    if not dmer_ids:
        return set()
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT dmer_id FROM dmer_processing WHERE dmer_id = ANY(%s)",
                (dmer_ids,),
            )
            known = {row[0] for row in cur.fetchall()}
    finally:
        conn.close()
    logger.info(
        "Checked %d dmer_id(s) against dmer_processing: %d already known",
        len(dmer_ids),
        len(known),
    )
    return known


def _record_dmer_started(
    dmer_id: str, driver_license: str | None, blob_path: str
) -> None:
    """Insert the tracking row for a newly-downloaded DMER.

    By the time this is called, `_already_known_dmer_ids` has already
    confirmed `dmer_id` isn't recorded yet, so this is normally a plain
    insert. `ON CONFLICT (dmer_id) DO NOTHING` is only a defensive safety
    net against a race (e.g. an overlapping poll) -- it deliberately does
    NOT reset an existing row, since that row may already have progressed
    past "started".
    """
    now = datetime.now(timezone.utc)
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            # Diagnostic: which role / database / search_path the
            # connection is operating as -- handy for debugging
            # permission issues (kept at INFO since the Azure portal
            # doesn't expose a DEBUG log level for this app).
            cur.execute(
                "SELECT current_user, current_database(), "
                "current_setting('search_path')"
            )
            logger.info("PG session context: %s", cur.fetchone())
            cur.execute(
                """
                    INSERT INTO dmer_processing (
                        dmer_id, driver_license, blob_path, status,
                        retry_count, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dmer_id) DO NOTHING
                    """,
                (dmer_id, driver_license, blob_path, "started", 0, now, now),
            )
    finally:
        conn.close()
    logger.info(
        "Recorded dmer_processing row for dmer_id=%s (status=started, driver_license=%s, blob_path=%s)",
        dmer_id,
        "present" if driver_license else "null",
        blob_path,
    )


def _record_dmer_failed(
    dmer_id: str, driver_license: str | None, error_message: str
) -> None:
    """Record a DMER that Mercury returned but that we couldn't download
    (missing document_url, download error, etc.), for manual triage.

    Unlike the previous filename-parsing design, `dmer_id` is always known
    here -- it's Mercury's own document_guid, present on every record
    regardless of whether the download succeeded.
    """
    now = datetime.now(timezone.utc)
    conn = _get_postgres_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """
                    INSERT INTO dmer_processing (
                        dmer_id, driver_license, status, error_message,
                        retry_count, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (dmer_id) DO NOTHING
                    """,
                (dmer_id, driver_license, "failed", error_message, 0, now, now),
            )
    finally:
        conn.close()
    logger.info(
        "Recorded dmer_processing row for dmer_id=%s (status=failed, error_message=%s)",
        dmer_id,
        error_message,
    )


def _process_mercury_records(records: list[dict], container_name: str) -> int:
    """Downloads and registers every DMER in `records` not already known to
    `dmer_processing`. Returns how many were new.

    Each record is handled independently: a failure on one (a download
    error, a transient DB error) is logged and processing moves on to the
    rest, rather than aborting the whole page -- the next time this link (or
    its successor, once retried) comes around, Mercury will still list
    anything we haven't managed to record yet.
    """
    if not records:
        logger.info("No records returned for this link; nothing to process")
        return 0

    dmer_ids = [record["document_guid"] for record in records]
    logger.info(
        "Processing %d record(s) from this link: dmer_ids=%s", len(dmer_ids), dmer_ids
    )
    known = _already_known_dmer_ids(dmer_ids)

    new_count = 0
    for record in records:
        dmer_id = record["document_guid"]
        if dmer_id in known:
            logger.info("dmer_id=%s already known; skipping", dmer_id)
            continue
        new_count += 1

        driver = record.get("driver")
        driver_license = (driver.get("licence_number") or None) if driver else None
        logger.info(
            "dmer_id=%s is new (driver_license=%s)",
            dmer_id,
            "present" if driver_license else "null (no driver attached)",
        )

        document_url = record.get("document_url")
        if not document_url:
            logger.error(
                "dmer_id=%s has no document_url in Mercury's response", dmer_id
            )
            try:
                _record_dmer_failed(
                    dmer_id, driver_license, "Mercury record has no document_url"
                )
            except Exception:
                logger.exception(
                    "Failed to record dmer_id=%s as failed; will retry next poll",
                    dmer_id,
                )
            continue

        try:
            blob_path, blob_url = _download_to_blob(
                document_url, dmer_id, container_name
            )
        except Exception as exc:
            logger.exception("Failed to download dmer_id=%s from Mercury", dmer_id)
            try:
                _record_dmer_failed(dmer_id, driver_license, f"Download failed: {exc}")
            except Exception:
                logger.exception(
                    "Failed to record dmer_id=%s as failed; will retry next poll",
                    dmer_id,
                )
            continue

        # Publish before recording the DB row (not after): this way, a
        # dmer_processing row only ever exists for a DMER di-processor has
        # actually been notified about. A publish failure here is treated
        # as retryable, not a permanent "failed" -- nothing is recorded, so
        # the next poll sees this dmer_id as still-unknown and retries the
        # whole thing (download included) from scratch. That's a deliberate
        # trade -- possible redundant downloads on a flaky Service Bus
        # connection -- over the alternative (a "started" row with no
        # message ever sent, silently stuck until someone notices).
        try:
            _publish_raw_dmer_message(dmer_id, driver_license, blob_url)
        except Exception:
            logger.exception(
                "Failed to publish raw-dmer-queue message for dmer_id=%s; will retry next poll",
                dmer_id,
            )
            continue

        try:
            _record_dmer_started(dmer_id, driver_license, blob_path)
        except Exception:
            logger.exception(
                "Failed to record dmer_processing row for dmer_id=%s; will retry next poll",
                dmer_id,
            )

    logger.info(
        "Finished processing this link's records: %d new out of %d returned",
        new_count,
        len(records),
    )
    return new_count


@app.timer_trigger(
    schedule="%DMER_POLL_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def dmer_intake(timer: func.TimerRequest) -> None:
    """Polls Mercury for one page of pending DMERs per invocation and
    downloads/registers any not already known to `dmer_processing`.

    Concurrency: only one invocation is ever allowed to do work at a time --
    see _acquire_poll_lock. If a previous invocation is still running when
    this one fires (e.g. it's running long), this one logs that fact and
    exits immediately; the next scheduled tick tries again.

    Pagination state (which link to call next) lives in `mercury_links` --
    see _get_current_link/_finish_link/_mark_link_not_started.
    """
    if timer.past_due:
        logger.warning("dmer_intake: this poll is running late (past_due)")

    logger.info("dmer_intake: poll triggered")

    lock_conn = _acquire_poll_lock()
    if lock_conn is None:
        logger.info(
            "dmer_intake: skipping this tick, another invocation is already polling"
        )
        return

    try:
        link_id, link, _status = _get_current_link()
        _mark_link_processing(link_id)

        try:
            body = _mercury_api_get(link)
        except Exception:
            logger.exception(
                "dmer_intake: Mercury API call failed for link_id=%s; resetting for retry",
                link_id,
            )
            _mark_link_not_started(link_id)
            return

        records = body.get("value", [])
        container_name = os.environ["DMER_RAW_CONTAINER"]

        try:
            new_count = _process_mercury_records(records, container_name)
        except Exception:
            logger.exception(
                "dmer_intake: unexpected error processing records for link_id=%s; "
                "resetting for retry",
                link_id,
            )
            _mark_link_not_started(link_id)
            return

        _finish_link(link_id, body.get("nextLink") or None)
        logger.info(
            "dmer_intake: poll complete for link_id=%s -- %d new DMER(s) out of %d returned",
            link_id,
            new_count,
            len(records),
        )
    finally:
        _release_poll_lock(lock_conn)
        logger.info("dmer_intake: poll finished")
