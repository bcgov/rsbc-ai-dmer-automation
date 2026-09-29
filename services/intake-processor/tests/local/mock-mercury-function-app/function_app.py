# mock-mercury-api — TEMPORARY test-only Azure Function App.
#
# Stands in for the real Mercury backlog API so intake-processor's poller
# can be exercised end-to-end against real Azure (VNet integration,
# Postgres AAD auth, real blob storage) without real Mercury credentials or
# a route from this dev machine into Mercury.
#
# Dynamic: every request lists the PDFs in this app's own storage account
# (container MOCK_DMER_CONTAINER, default "mock-dmers") and returns one
# Mercury-shaped record per blob. Each record's document_url is a freshly
# signed, read-only, single-blob SAS URL -- the Azure equivalent of an S3
# presigned URL -- expiring after MOCK_URL_TTL_MINUTES (default 60). To add
# or remove test DMERs, just upload to / delete from that container.
#
# Why this app's own storage account (the one in AzureWebJobsStorage) and
# not rsbcstorage: this app runs on a Linux Consumption plan, which can't
# join the VNet, and rsbcstorage only accepts traffic through its private
# endpoints -- so this app can neither list nor sign against it. This
# account accepts public traffic but has anonymous blob access disabled, so
# a blob is only readable through a signed URL, like a private S3 bucket.
#
# Stable identity across polls: document_guid is a uuid5 of the blob name,
# so the same file always yields the same guid and re-polling dedupes the
# same way real Mercury data would. Driver/case details are likewise derived
# from the guid. A blob whose name contains "nodriver" gets `driver: null`,
# to exercise intake-processor's no-driver path.
#
# NOT part of the DMER pipeline. Delete this Function App (and its storage
# account, stmockmercuryapi01) once Azure testing is done.

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import azure.functions as func
from azure.storage.blob import BlobSasPermissions, BlobServiceClient, generate_blob_sas

app = func.FunctionApp()

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100
# Signed URLs start slightly in the past so a small clock difference between
# this app and the downloader doesn't make a brand-new URL "not yet valid".
_SAS_CLOCK_SKEW = timedelta(minutes=5)


def _int_param(req: func.HttpRequest, name: str, default: int) -> int:
    try:
        return int(req.params.get(name, default))
    except (TypeError, ValueError):
        return default


def _signed_url(service: BlobServiceClient, container: str, blob_name: str, now: datetime) -> str:
    ttl = timedelta(minutes=int(os.environ.get("MOCK_URL_TTL_MINUTES", "60")))
    sas = generate_blob_sas(
        account_name=service.account_name,
        container_name=container,
        blob_name=blob_name,
        account_key=service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        start=now - _SAS_CLOCK_SKEW,
        expiry=now + ttl,
    )
    return f"{service.get_blob_client(container, blob_name).url}?{sas}"


def _record(blob_name: str, last_modified: datetime, document_url: str) -> dict:
    guid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-mercury/{blob_name}"))
    stem = blob_name.rsplit(".", 1)[0]
    received = last_modified.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    licence = str(int(guid.replace("-", "")[:12], 16) % 10_000_000).zfill(7)
    driver = None
    if "nodriver" not in blob_name.lower():
        driver = {
            "driver_id": f"D{licence}",
            "first_name": "Mock",
            "middle_name": "",
            "last_name": stem,
            "licence_number": licence,
            "documents": [
                {
                    "file_name": stem,
                    "document_type": "DMER",
                    "document_status": "Uploaded",
                    "uploaded_date": received,
                    "document_url": document_url,
                    "dps_date": "",
                }
            ],
        }
    return {
        "dps_queue": "General",
        "document_guid": guid,
        "document_name": blob_name,
        "document_type": "DMER",
        "document_status": "Uploaded",
        "document_priority": "Regular",
        "received_date": received,
        "document_type_business_area": "Driver Fitness",
        "queue": "Team - Intake",
        "dmer_status": "",
        "document_url": document_url,
        "driver": driver,
        "case": {
            "case_id": f"C{guid[:8].upper()}",
            "case_title": f"{stem} - RSBC - 1",
            "case_type": "RSBC",
            "case_priority": "Regular",
            "case_owner": "Team - Intake",
            "case_status": "Open Pending Submission",
        },
    }


@app.route(route="mercury/documents", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def mercury_documents(req: func.HttpRequest) -> func.HttpResponse:
    queue = req.params.get("queue", "BOTH")
    page_size = max(1, min(_int_param(req, "page_size", _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE))
    # A non-numeric cursor (e.g. "page2" from the previous static mock, still
    # sitting in a poll_checkpoint row) restarts from the first page.
    offset = max(0, _int_param(req, "cursor", 0))

    container = os.environ.get("MOCK_DMER_CONTAINER", "mock-dmers")
    service = BlobServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"])
    blobs = sorted(
        (b for b in service.get_container_client(container).list_blobs() if b.name.lower().endswith(".pdf")),
        key=lambda b: b.name,
    )

    now = datetime.now(UTC)
    records = [
        _record(b.name, b.last_modified, _signed_url(service, container, b.name, now))
        for b in blobs[offset : offset + page_size]
    ]

    next_offset = offset + page_size
    next_link = None
    if next_offset < len(blobs):
        base_url = req.url.split("/api/")[0]
        query = urlencode({"queue": queue, "page_size": page_size, "cursor": next_offset})
        next_link = f"{base_url}/api/mercury/documents?{query}"

    return func.HttpResponse(
        json.dumps({"value": records, "nextLink": next_link}),
        mimetype="application/json",
        status_code=200,
    )
