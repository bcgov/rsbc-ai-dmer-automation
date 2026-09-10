# intake-processor — Azure Functions (Python v2 model)
#
# Event Grid-triggered ingestion: when a new DMER PDF lands in the storage
# container, Azure Storage emits a `Microsoft.Storage.BlobCreated` event which
# Event Grid routes to this function. The blob's filename is parsed for its
# DMER id and driver's licence number and a tracking row is inserted into
# `dmer_processing` with status="started" for the downstream pipeline
# (paddle/DI processors) to pick up.
#
# An Event Grid subscription on the storage account must route
# `Microsoft.Storage.BlobCreated` events to this function, filtered (via the
# subscription's subject filter) to the intake container. This function only
# ever needs the blob's filename, so it reads the event and never downloads
# the blob itself.
#
# See docs/services/intake-processor.md for the service's full responsibilities.

from __future__ import annotations

import logging
import os
import re
import urllib.parse
from datetime import datetime, timezone

import azure.functions as func
import psycopg2

app = func.FunctionApp()

logger = logging.getLogger(__name__)

# Matches "DMER0001 - <driver license>.pdf" (case-insensitive, tolerant of
# extra whitespace around the separating hyphen). Any leading folder path in
# the blob name is stripped before matching.
_DMER_FILENAME_RE = re.compile(
    r"^(?P<dmer_id>DMER\d+)\s*-\s*(?P<driver_license>.+?)\s*\.pdf$",
    re.IGNORECASE,
)

# Token scope for AAD/Managed Identity auth against Azure Database for
# PostgreSQL Flexible Server.
_AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def _parse_dmer_filename(blob_name: str) -> tuple[str, str]:
    """Extract (dmer_id, driver_license) from a blob name such as
    "DMER0001 - 1234567.pdf".
    """
    filename = os.path.basename(blob_name)
    match = _DMER_FILENAME_RE.match(filename)
    if not match:
        raise ValueError(
            f"Blob name '{filename}' does not match the expected "
            "'DMER<id> - <driver license>.pdf' pattern"
        )
    return match.group("dmer_id"), match.group("driver_license")


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
        # Lazy import: azure-identity is only needed on the Managed Identity path.
        from azure.identity import DefaultAzureCredential

        password = DefaultAzureCredential().get_token(_AAD_POSTGRES_SCOPE).token

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=database,
        user=user,
        password=password,
        sslmode=sslmode,
    )


def _record_dmer_started(dmer_id: str, driver_license: str, blob_path: str) -> None:
    """Insert the tracking row for a newly-received DMER.

    Uses an upsert on the unique `dmer_id` so a duplicate event delivery
    (Event Grid at-least-once delivery, blob re-upload) resets the row back to
    "started" instead of failing on the unique constraint.
    """
    now = datetime.now(timezone.utc)
    conn = _get_postgres_connection()
    try:
        with conn:
            with conn.cursor() as cur:
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
                    ON CONFLICT (dmer_id) DO UPDATE SET
                        driver_license = EXCLUDED.driver_license,
                        blob_path      = EXCLUDED.blob_path,
                        status         = EXCLUDED.status,
                        updated_at     = EXCLUDED.updated_at
                    """,
                    (dmer_id, driver_license, blob_path, "started", 0, now, now),
                )
    finally:
        conn.close()


def _record_parse_failure(blob_path: str, error_message: str) -> None:
    """Record a blob whose filename didn't match the expected DMER pattern.

    `dmer_id` is left NULL (the UNIQUE constraint allows multiple NULLs) since
    none could be extracted -- the row exists purely so an unparseable
    filename is visible in `dmer_processing` (status="failed", with the
    reason in `error_message`) for manual triage, rather than the event
    being retried forever / dead-lettered.
    """
    now = datetime.now(timezone.utc)
    conn = _get_postgres_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO dmer_processing (
                        dmer_id, blob_path, status, error_message,
                        retry_count, created_at, updated_at
                    )
                    VALUES (NULL, %s, %s, %s, %s, %s, %s)
                    """,
                    (blob_path, "failed", error_message, 0, now, now),
                )
    finally:
        conn.close()


@app.event_grid_trigger(arg_name="event")
def dmer_intake(event: func.EventGridEvent) -> None:
    """Triggered by an Event Grid `Microsoft.Storage.BlobCreated` event for a
    new DMER PDF.

    Parses the DMER id and driver's licence number out of the blob's filename
    and records the intake as a "started" row in `dmer_processing`.
    """
    if event.event_type != "Microsoft.Storage.BlobCreated":
        logger.info("dmer_intake ignoring event type '%s'", event.event_type)
        return

    data = event.get_json() or {}
    # `data.url` is the canonical blob URL (percent-encoded); `event.subject`
    # is the un-encoded "/blobServices/default/containers/<c>/blobs/<name>"
    # path. Parse the filename from the subject, store the URL for downstream.
    raw_url = data.get("url") or ""
    blob_path = urllib.parse.unquote(raw_url) if raw_url else (event.subject or "")
    logger.info("dmer_intake received BlobCreated for '%s'", blob_path)

    try:
        dmer_id, driver_license = _parse_dmer_filename(event.subject or blob_path)
    except ValueError as exc:
        logger.exception(
            "Unable to parse DMER id / driver licence from '%s'", event.subject
        )
        # A malformed filename will never parse no matter how many times the
        # event is retried, so record it as failed instead of raising (which
        # would just retry and eventually dead-letter). If even the failure
        # record can't be written, re-raise so it's retried rather than lost.
        try:
            _record_parse_failure(blob_path, str(exc))
        except Exception:
            logger.exception(
                "Failed to record parse failure for '%s'; will retry", blob_path
            )
            raise
        return

    try:
        _record_dmer_started(dmer_id, driver_license, blob_path)
    except Exception:
        logger.exception("Failed to record dmer_processing row for dmer_id=%s", dmer_id)
        raise

    logger.info("Recorded dmer_processing row for dmer_id=%s (status=started)", dmer_id)
