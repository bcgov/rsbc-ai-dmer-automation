# mock-mercury-api — TEMPORARY test-only Azure Function App.
#
# Stands in for the real Mercury APIs so the pipeline can be exercised
# end-to-end against real Azure (VNet integration, Postgres AAD auth, real
# blob storage) without real Mercury credentials or a route from this dev
# machine into Mercury.
#
# Two endpoints:
#
#   GET /api/mercury/documents?queue=&page_size=&cursor=
#       The batch/backlog list the Page Poller reads (one record per DMER).
#   GET /api/mercury/drivers/{licence_number}
#       The driver-licence lookup Driver Lookup reads: the driver, every
#       active document Mercury holds for them, and their case. 404 when no
#       driver has that licence. 7- and 8-digit forms of a licence match.
#
# Dynamic: every request lists the PDFs in this app's own storage account
# (container MOCK_DMER_CONTAINER, default "mock-dmers"). Each document_url is
# a freshly signed, read-only, single-blob SAS URL -- the Azure equivalent of
# an S3 presigned URL -- expiring after MOCK_URL_TTL_MINUTES (default 60). To
# add or remove test documents, just upload to / delete from that container.
#
# How blob names shape the mock data:
#
# - A PDF at the top level ("DMER2_.pdf") is its own driver with one document
#   -- the original behaviour.
# - PDFs in a folder ("drv-a/DMER-0001.pdf", "drv-a/report.pdf") share one
#   driver and case: the folder is the driver.
# - "nodriver" in the name: no driver (`driver: null` in the batch list).
# - "report" in the file name: document_type "Test Report". These appear in
#   the driver's active_documents but not in the batch list, which is DMERs.
# - "rejected" in the file name: document_status "Rejected", with a dps_date.
#
# Stable identity across polls: document_guid is a uuid5 of the blob name and
# a driver's licence a hash of its folder (or blob) name, so the same files
# always yield the same ids and re-polling dedupes like real Mercury data.
#
# Why this app's own storage account (the one in AzureWebJobsStorage) and
# not rsbcstorage: this app runs on a Linux Consumption plan, which can't
# join the VNet, and rsbcstorage only accepts traffic through its private
# endpoints -- so this app can neither list nor sign against it. This
# account accepts public traffic but has anonymous blob access disabled, so
# a blob is only readable through a signed URL, like a private S3 bucket.
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
_ISO = "%Y-%m-%dT%H:%M:%SZ"


def _int_param(req: func.HttpRequest, name: str, default: int) -> int:
    try:
        return int(req.params.get(name, default))
    except (TypeError, ValueError):
        return default


def _service() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"])


def _container() -> str:
    return os.environ.get("MOCK_DMER_CONTAINER", "mock-dmers")


def _pdfs(service: BlobServiceClient) -> list:
    return sorted(
        (
            b
            for b in service.get_container_client(_container()).list_blobs()
            if b.name.lower().endswith(".pdf")
        ),
        key=lambda b: b.name,
    )


def _signed_url(service: BlobServiceClient, blob_name: str, now: datetime) -> str:
    ttl = timedelta(minutes=int(os.environ.get("MOCK_URL_TTL_MINUTES", "60")))
    sas = generate_blob_sas(
        account_name=service.account_name,
        container_name=_container(),
        blob_name=blob_name,
        account_key=service.credential.account_key,
        permission=BlobSasPermissions(read=True),
        start=now - _SAS_CLOCK_SKEW,
        expiry=now + ttl,
    )
    return f"{service.get_blob_client(_container(), blob_name).url}?{sas}"


def _file_name(blob_name: str) -> str:
    return blob_name.rsplit("/", 1)[-1]


def _stem(blob_name: str) -> str:
    return _file_name(blob_name).rsplit(".", 1)[0]


def _guid(blob_name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-mercury/{blob_name}"))


def _driver_group(blob_name: str) -> str | None:
    """The folder (shared driver) or, at the top level, the blob itself.
    None for a "nodriver" blob."""
    if "nodriver" in blob_name.lower():
        return None
    return blob_name.split("/", 1)[0] if "/" in blob_name else blob_name


def _licence(group: str) -> str:
    # A top-level blob keeps the licence the original mock gave it.
    seed = (
        _guid(group)
        if _is_single_blob(group)
        else str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-mercury-driver/{group}"))
    )
    return str(int(seed.replace("-", "")[:12], 16) % 10_000_000).zfill(7)


def _document_type(blob_name: str) -> str:
    return "Test Report" if "report" in _file_name(blob_name).lower() else "DMER"


def _received(blob) -> str:
    return blob.last_modified.astimezone(UTC).strftime(_ISO)


def _document(blob, document_url: str) -> dict:
    rejected = "rejected" in _file_name(blob.name).lower()
    document = {
        "file_name": _stem(blob.name),
        "document_type": _document_type(blob.name),
        "document_status": "Rejected" if rejected else "Uploaded",
        "uploaded_date": _received(blob),
        "document_url": document_url,
    }
    if rejected:
        triaged = blob.last_modified.astimezone(UTC) + timedelta(days=1)
        document["dps_date"] = triaged.strftime(_ISO)
    return document


def _is_single_blob(group: str) -> bool:
    return "/" not in group and group.lower().endswith(".pdf")


def _case(group: str) -> dict:
    if _is_single_blob(group):
        # The case the original mock gave a top-level blob.
        case_id, title = f"C{_guid(group)[:8].upper()}", f"{_stem(group)} - RSBC - 1"
    else:
        licence = _licence(group)
        case_id, title = f"C{licence}", f"{licence} - {_stem(group).upper()} - RSBC - 1"
    return {
        "case_id": case_id,
        "case_title": title,
        "case_type": "RSBC",
        "case_priority": "Regular",
        "case_owner": "Team - Intake",
        "case_status": "Open Pending Submission",
    }


def _driver_identity(group: str) -> dict:
    licence = _licence(group)
    return {
        "driver_id": f"D{licence}",
        "first_name": "Mock",
        "middle_name": "",
        "last_name": _stem(group),
        "licence_number": licence,
    }


def _batch_record(blob, document_url: str, group_documents: list[dict]) -> dict:
    guid = _guid(blob.name)
    group = _driver_group(blob.name)
    driver = None
    if group is not None:
        # The batch shape's driver documents keep their own key name and dps_date.
        documents = [{"dps_date": "", **d} for d in group_documents]
        driver = {**_driver_identity(group), "documents": documents}
    case = (
        _case(group)
        if group is not None
        else {
            "case_id": f"C{guid[:8].upper()}",
            "case_title": f"{_stem(blob.name)} - RSBC - 1",
            "case_type": "RSBC",
            "case_priority": "Regular",
            "case_owner": "Team - Intake",
            "case_status": "Open Pending Submission",
        }
    )
    return {
        "dps_queue": "General",
        "document_guid": guid,
        "document_name": _file_name(blob.name),
        "document_type": "DMER",
        "document_status": "Uploaded",
        "document_priority": "Regular",
        "received_date": _received(blob),
        "document_type_business_area": "Driver Fitness",
        "queue": "Team - Intake",
        "dmer_status": "",
        "document_url": document_url,
        "driver": driver,
        "case": case,
    }


def _documents_by_group(service: BlobServiceClient, blobs: list, now: datetime):
    """Each blob's signed URL, and each driver group's document list."""
    urls = {b.name: _signed_url(service, b.name, now) for b in blobs}
    groups: dict[str, list[dict]] = {}
    for blob in blobs:
        group = _driver_group(blob.name)
        if group is not None:
            groups.setdefault(group, []).append(_document(blob, urls[blob.name]))
    return urls, groups


@app.route(
    route="mercury/documents", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS
)
def mercury_documents(req: func.HttpRequest) -> func.HttpResponse:
    queue = req.params.get("queue", "BOTH")
    page_size = max(
        1, min(_int_param(req, "page_size", _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE)
    )
    # A non-numeric cursor (e.g. "page2" from the previous static mock, still
    # sitting in a poll_checkpoint row) restarts from the first page.
    offset = max(0, _int_param(req, "cursor", 0))

    service = _service()
    blobs = _pdfs(service)
    dmers = [b for b in blobs if _document_type(b.name) == "DMER"]
    urls, groups = _documents_by_group(service, blobs, datetime.now(UTC))
    records = [
        _batch_record(b, urls[b.name], groups.get(_driver_group(b.name) or "", []))
        for b in dmers[offset : offset + page_size]
    ]

    next_offset = offset + page_size
    next_link = None
    if next_offset < len(dmers):
        base_url = req.url.split("/api/")[0]
        query = urlencode(
            {"queue": queue, "page_size": page_size, "cursor": next_offset}
        )
        next_link = f"{base_url}/api/mercury/documents?{query}"

    return func.HttpResponse(
        json.dumps({"value": records, "nextLink": next_link}),
        mimetype="application/json",
        status_code=200,
    )


def _same_licence(a: str, b: str) -> bool:
    """7- and 8-digit forms of a BC licence are the same licence."""
    digits_a, digits_b = "".join(c for c in a if c.isdigit()), "".join(
        c for c in b if c.isdigit()
    )
    return bool(digits_a) and digits_a.lstrip("0") == digits_b.lstrip("0")


@app.route(
    route="mercury/drivers/{licence_number}",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_driver(req: func.HttpRequest) -> func.HttpResponse:
    licence_number = req.route_params.get("licence_number", "")
    service = _service()
    blobs = _pdfs(service)
    _, groups = _documents_by_group(service, blobs, datetime.now(UTC))
    for group, documents in groups.items():
        if _same_licence(_licence(group), licence_number):
            body = {
                **_driver_identity(group),
                "active_documents": documents,
                "case": _case(group),
            }
            return func.HttpResponse(
                json.dumps(body), mimetype="application/json", status_code=200
            )
    return func.HttpResponse(
        json.dumps({"error": "driver not found"}),
        mimetype="application/json",
        status_code=404,
    )
