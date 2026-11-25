# mock-mercury-api — TEMPORARY test-only Azure Function App.
#
# Stands in for the real Mercury APIs so the pipeline can be exercised
# end-to-end against real Azure (VNet integration, Postgres AAD auth, real
# blob storage) without real Mercury credentials or a route from this dev
# machine into Mercury.
#
# Read endpoints (write-back endpoints for the Outbox Publisher are at the end):
#
#   GET  /api/mercury/Authentication/token?secret=
#       A bearer token ({"token", "expiresIn"}). 401 for a wrong secret when
#       MOCK_MERCURY_SECRET is set; the other endpoints don't check tokens.
#   GET  /api/mercury/dmers?count=
#       Mercury's GET dmers, in its shape (camelCase "items"): the first
#       count documents not yet marked AI Processed -- DMERs and, like
#       Mercury, other document types (no documentUrl). Claim-based: the
#       same call returns the next ones once these are marked.
#       documentUrl is a relative path, as in Mercury -- not a link.
#   GET  /api/mercury/dmers/{documentRecordId}/files
#       The DMER's PDF bytes (application/pdf); 404 for an unknown id.
#   POST /api/mercury/dmers/BatchSetDmerAIProccessedStatus  {"dmerIds": [...]}
#       Marks documents AI Processed: one blob per guid in
#       MOCK_AI_PROCESSED_CONTAINER (default "mock-ai-processed"). Delete a
#       blob there to make that document come back.
#   POST /api/mercury/drivers/lookup  {"driverLicenceNumber": "..."}
#       The driver-licence lookup Driver Lookup reads, in Mercury's shape
#       (Mercury DEV's Swagger, 2026-11-24): the driver in snake_case, their
#       "documents" in camelCase (fileName, documentType, submittalStatus,
#       uploadedDate, documentRecordId), and their open case as "activeCase"
#       (camelCase). 404 when no driver has that licence. 7- and 8-digit forms
#       of a licence match.
#
# Dynamic: every request lists the PDFs in this app's own storage account
# (container MOCK_DMER_CONTAINER, default "mock-dmers"). To add or remove test
# documents, just upload to / delete from that container.
#
# How blob names shape the mock data:
#
# - A PDF at the top level ("DMER2_.pdf") is its own driver with one document
#   -- the original behaviour.
# - PDFs in a folder ("drv-a/DMER-0001.pdf", "drv-a/report.pdf") share one
#   driver and case: the folder is the driver.
# - "nodriver" in the name: no driver (`driver: null` in GET dmers).
# - "report" in the file name: document_type "Test Report". These appear in
#   the driver's documents, and in GET dmers with an empty
#   documentUrl, as Mercury's non-DMER items do.
# - "rejected" in the file name: document_status "Rejected", with a dps_date.
# - "nocase" in the driver's folder (or top-level blob) name: no case -- no
#   dmerDocumentCaseRecord in GET dmers, "activeCase": null in the lookup --
#   so the pipeline's CREATE_CASE calls add_case for it.
#
# Stable identity across polls: document_guid is a uuid5 of the blob name and
# a driver's licence a hash of its folder (or blob) name, so the same files
# always yield the same ids and re-polling dedupes like real Mercury data.
#
# Why this app's own storage account (the one in AzureWebJobsStorage) and
# not rsbcstorage: this app runs on a Linux Consumption plan, which can't
# join the VNet, and rsbcstorage only accepts traffic through its private
# endpoints -- so this app can't list it. This account has anonymous blob
# access disabled; PDFs are served only through GET dmers/{id}/files.
#
# NOT part of the DMER pipeline. Delete this Function App (and its storage
# account, stmockmercuryapi01) once Azure testing is done.

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import azure.functions as func
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

_DEFAULT_PAGE_SIZE = 50
_MAX_PAGE_SIZE = 100
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


def _document(blob) -> dict:
    """One of the driver's documents, as POST drivers/lookup returns it."""
    rejected = "rejected" in _file_name(blob.name).lower()
    return {
        "fileName": _stem(blob.name),
        "documentType": _document_type(blob.name),
        "submittalStatus": "Rejected" if rejected else "Uploaded",
        "uploadedDate": _received(blob),
        "documentRecordId": _guid(blob.name),
    }


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


def _camel_case_record(case: dict) -> dict:
    """A case in Mercury's camelCase (GET dmers' dmerDocumentCaseRecord, the
    lookup's activeCase)."""
    return {
        "caseId": case["case_id"],
        "caseTitle": case["case_title"],
        "caseType": case["case_type"],
        "casePriority": case["case_priority"],
        "caseOwner": case["case_owner"],
        "caseStatus": case["case_status"],
    }


def _has_case(group: str) -> bool:
    return "nocase" not in group.lower()


def _driver_identity(group: str) -> dict:
    licence = _licence(group)
    return {
        "driver_id": f"D{licence}",
        "first_name": "Mock",
        "middle_name": "",
        "last_name": _stem(group),
        "licence_number": licence,
    }


# Mercury's receivedDate: "MM/DD/YYYY HH:MM:SS" with no zone -- written here
# in Pacific time, the zone the pipeline reads it in by default.
_MERCURY_DATE = "%m/%d/%Y %H:%M:%S"
_PACIFIC = ZoneInfo("America/Vancouver")


def _dmers_item(blob, document_url: str) -> dict:
    """One GET dmers item, in Mercury's camelCase shape. A non-DMER (a
    "report" blob) has no documentUrl, as in Mercury."""
    guid = _guid(blob.name)
    group = _driver_group(blob.name)
    is_dmer = _document_type(blob.name) == "DMER"
    driver = None
    if group is not None:
        identity = _driver_identity(group)
        driver = {
            "driverId": identity["driver_id"],
            "firstName": identity["first_name"],
            "middleName": identity["middle_name"],
            "lastName": identity["last_name"],
            "licenceNumber": identity["licence_number"],
            "documents": [],
        }
    case = (
        (_case(group) if _has_case(group) else None)
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
        "documentRecordId": guid,
        "queue": "Team - Intake",
        "documentName": _file_name(blob.name),
        "documentUrl": document_url if is_dmer else "",
        "documentType": _document_type(blob.name),
        "submittalStatus": "Uploaded",
        "documentPriority": "Regular",
        "receivedDate": blob.last_modified.astimezone(_PACIFIC).strftime(_MERCURY_DATE),
        "documentTypeBusinessArea": "Driver Fitness" if is_dmer else "",
        "dmerStatus": "Uploaded",
        "driver": driver,
        **({"dmerDocumentCaseRecord": _camel_case_record(case)} if case else {}),
    }


def _ai_processed_container():
    from azure.core.exceptions import ResourceExistsError

    container = _service().get_container_client(
        os.environ.get("MOCK_AI_PROCESSED_CONTAINER", "mock-ai-processed")
    )
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    return container


def _ai_processed() -> set[str]:
    return {b.name for b in _ai_processed_container().list_blobs()}


def _documents_by_group(blobs: list) -> dict[str, list[dict]]:
    """Each driver group's document list."""
    groups: dict[str, list[dict]] = {}
    for blob in blobs:
        group = _driver_group(blob.name)
        if group is not None:
            groups.setdefault(group, []).append(_document(blob))
    return groups


@app.route(
    route="mercury/Authentication/token",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_token(req: func.HttpRequest) -> func.HttpResponse:
    """Mercury's GET Authentication/token?secret=: a bearer token for the
    other calls. When MOCK_MERCURY_SECRET is set, any other secret gets a
    401. The other mock endpoints do not check the token."""
    expected = os.environ.get("MOCK_MERCURY_SECRET", "")
    if expected and req.params.get("secret") != expected:
        return _json({"error": "invalid secret"}, 401)
    ttl = int(os.environ.get("MOCK_TOKEN_TTL_SECONDS", "3600"))
    return _json({"token": f"mock-{uuid.uuid4()}", "expiresIn": ttl})


@app.route(route="mercury/dmers", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def mercury_dmers(req: func.HttpRequest) -> func.HttpResponse:
    # Mercury's page size is "count" (Mercury ignores pageSize).
    page_size = max(
        1, min(_int_param(req, "count", _DEFAULT_PAGE_SIZE), _MAX_PAGE_SIZE)
    )
    service = _service()
    marked = _ai_processed()
    waiting = [b for b in _pdfs(service) if _guid(b.name) not in marked]
    page = waiting[:page_size]
    # Mercury's documentUrl is a path inside Mercury, not a link; the file
    # comes from GET dmers/{documentRecordId}/files (mercury_dmer_file).
    items = [_dmers_item(b, f"dfp_driver/mock/{_file_name(b.name)}") for b in page]
    # Like Mercury: no nextLink; "count" echoes the page size asked for.
    return func.HttpResponse(
        json.dumps({"items": items, "count": page_size}),
        mimetype="application/json",
        status_code=200,
    )


@app.route(
    route="mercury/dmers/{guid}/files",
    methods=["GET"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_dmer_file(req: func.HttpRequest) -> func.HttpResponse:
    """The DMER's PDF by its documentRecordId, as Mercury's
    GET dmers/{documentRecordId}/files. 404 when no mock blob has that id."""
    blob_name = _source_blob(req.route_params.get("guid", ""))
    if blob_name is None:
        return _json({"error": "document not found"}, 404)
    content = (
        _service().get_blob_client(_container(), blob_name).download_blob().readall()
    )
    return func.HttpResponse(content, mimetype="application/pdf", status_code=200)


@app.route(
    route="mercury/dmers/BatchSetDmerAIProccessedStatus",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_set_ai_processed(req: func.HttpRequest) -> func.HttpResponse:
    fail = os.environ.get("MOCK_WRITE_FAIL_STATUS", "").strip()
    if fail:
        return _json({"error": "simulated outage"}, int(fail))
    try:
        ids = req.get_json().get("dmerIds")
    except (ValueError, AttributeError):
        return _json({"error": "body must be JSON with dmerIds"}, 400)
    if not isinstance(ids, list) or not all(isinstance(i, str) and i for i in ids):
        return _json({"error": "dmerIds must be a list of ids"}, 400)
    container = _ai_processed_container()
    received_at = datetime.now(UTC).strftime(_ISO)
    sources = {_guid(b.name): b.name for b in _pdfs(_service())}
    for guid in ids:
        record = {"received_at": received_at, "source_blob": sources.get(guid)}
        container.get_blob_client(guid).upload_blob(json.dumps(record), overwrite=True)
    return _json({"ok": True, "updated": len(ids)})


def _same_licence(a: str, b: str) -> bool:
    """7- and 8-digit forms of a BC licence are the same licence."""
    digits_a, digits_b = "".join(c for c in a if c.isdigit()), "".join(
        c for c in b if c.isdigit()
    )
    return bool(digits_a) and digits_a.lstrip("0") == digits_b.lstrip("0")


@app.route(
    route="mercury/drivers/lookup",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_driver(req: func.HttpRequest) -> func.HttpResponse:
    """Mercury's POST drivers/lookup {"driverLicenceNumber": ...}."""
    try:
        licence_number = str(req.get_json().get("driverLicenceNumber") or "")
    except (ValueError, AttributeError):
        return _json({"error": "body must be JSON with driverLicenceNumber"}, 400)
    if not licence_number:
        return _json({"error": "driverLicenceNumber is required"}, 400)
    groups = _documents_by_group(_pdfs(_service()))
    for group, documents in groups.items():
        if _same_licence(_licence(group), licence_number):
            body = {
                **_driver_identity(group),
                "documents": documents,
                "activeCase": (
                    _camel_case_record(_case(group)) if _has_case(group) else None
                ),
            }
            return func.HttpResponse(
                json.dumps(body), mimetype="application/json", status_code=200
            )
    return func.HttpResponse(
        json.dumps({"error": "driver not found"}),
        mimetype="application/json",
        status_code=404,
    )


# ---------------------------------------------------------------------------
# Write-back (the Outbox Publisher); these mirror what
# dmer_common.mercury_client.MercuryClient.send_outbox sends:
#
#   POST /api/mercury/dmers/{guid}/setDmerAIProccessedStatus  UPDATE_OUTCOME
#       Mercury's API (DMER in the path): {"aiProcessedStatus", "aiDpsPriority",
#       "aiQueue", "aiDmerStatus", "aiIsDuplicate", "notes"}; 400 without an
#       integer aiProcessedStatus.
#   POST /api/mercury/dmers/{guid}/add_driver                  MAP_DRIVER
#       Mercury's API: {"driverLicenceNumber", "firstName", "surname",
#       "dmerId", ...}; answers {"resultMessage", "driverOpenCase"} (the mock
#       driver's case when the licence is one of its drivers), or Mercury's
#       {"errorStatus": 1, "replyMessage", "caseId"} when a required field is
#       missing.
#
#   POST /api/mercury/dmers/{guid}/add_case                    CREATE_CASE
#       Mercury's API: {"caseType", "owner", "driver": {"driverId":
#       <licence>}, "documentType", "comments"} (caseType, documentType and
#       driver required: 400 otherwise); answers 201 {"caseId",
#       "documentRecordId", "status": "Created"}. 404 when no mock driver has
#       that licence; 409 when the DMER belongs to a different driver, or
#       already has a case (its driver's folder has no "nocase"). Errors as
#       Mercury's ProblemDetails ({"title", "status", "detail"}).
#
# Every write is recorded as a JSON blob in MOCK_WRITE_CONTAINER (default
# "mock-writes") on this app's storage account, named after the source PDF so
# a batch reads back as one folder:
#
#   <source blob name without .pdf>/<what>/<idempotency key>.json
#   e.g. t20261016e-DMER5_/outcome/<key>.json -- list prefix "t20261016e-"
#
# (a guid that matches no PDF in MOCK_DMER_CONTAINER goes under _unknown/<guid>/).
# Each record holds the source blob, the document_guid, the request body and the
# mock's response. A repeated Idempotency-Key returns the first response with
# "duplicate": true -- the behaviour the outbox assumes.
#
# Outage simulation: set MOCK_WRITE_FAIL_STATUS (e.g. 503) and every write
# returns that status until the setting is removed.
# ---------------------------------------------------------------------------


def _write_container():
    from azure.core.exceptions import ResourceExistsError

    container = _service().get_container_client(
        os.environ.get("MOCK_WRITE_CONTAINER", "mock-writes")
    )
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    return container


def _json(body: dict, status: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(body), mimetype="application/json", status_code=status
    )


def _problem(status: int, detail: str) -> func.HttpResponse:
    """An error as Mercury's ProblemDetails."""
    titles = {400: "Bad Request", 404: "Not Found", 409: "Conflict"}
    return _json(
        {"title": titles.get(status, "Error"), "status": status, "detail": detail},
        status,
    )


def _source_blob(guid: str) -> str | None:
    """The mock-dmers blob whose document_guid is *guid* (guids are uuid5 of
    the blob name, so this is a scan, not a lookup)."""
    for blob in _pdfs(_service()):
        if _guid(blob.name) == guid:
            return blob.name
    return None


def _record_write(
    req: func.HttpRequest, guid: str, what: str, extra: dict | None = None
) -> func.HttpResponse:
    from azure.core.exceptions import ResourceExistsError

    fail = os.environ.get("MOCK_WRITE_FAIL_STATUS", "").strip()
    if fail:
        return _json({"error": "simulated outage"}, int(fail))
    try:
        body = req.get_json()
    except ValueError:
        return _json({"error": "body must be JSON"}, 400)
    key = req.headers.get("Idempotency-Key") or uuid.uuid4().hex
    safe_key = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    source = _source_blob(guid)
    folder = source[: -len(".pdf")] if source else f"_unknown/{guid}"
    blob = _write_container().get_blob_client(f"{folder}/{what}/{safe_key}.json")
    response = {"ok": True, "document_guid": guid, "operation": what, **(extra or {})}
    record = {
        "received_at": datetime.now(UTC).strftime(_ISO),
        "source_blob": source,
        "document_guid": guid,
        "method": req.method,
        "idempotency_key": key,
        "body": body,
        "response": response,
    }
    try:
        blob.upload_blob(json.dumps(record), overwrite=False)
    except ResourceExistsError:
        first = json.loads(blob.download_blob().readall())
        return _json({**first["response"], "duplicate": True})
    return _json(response, 201 if req.method == "POST" else 200)


@app.route(
    route="mercury/dmers/{guid}/setDmerAIProccessedStatus",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_set_ai_status(req: func.HttpRequest) -> func.HttpResponse:
    """Mercury's POST Dmers/{dmerId}/setDmerAIProccessedStatus: the AI's
    outcome on one DMER, recorded under <source>/ai-status/."""
    try:
        body = req.get_json()
    except ValueError:
        return _problem(400, "body must be JSON")
    if not isinstance(body, dict) or not isinstance(body.get("aiProcessedStatus"), int):
        return _problem(400, "aiProcessedStatus (integer) is required")
    return _record_write(req, req.route_params.get("guid", ""), "ai-status")


_ADD_DRIVER_REQUIRED = ("driverLicenceNumber", "firstName", "surname", "dmerId")


@app.route(
    route="mercury/dmers/{guid}/add_driver",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_add_driver(req: func.HttpRequest) -> func.HttpResponse:
    """Mercury's POST dmers/{documentRecordId}/add_driver: link a driver to
    the DMER, answering with the driver's open case. Recorded under
    <source>/add-driver/."""
    try:
        body = req.get_json()
    except ValueError:
        body = None
    missing = [
        k for k in _ADD_DRIVER_REQUIRED if not str((body or {}).get(k) or "").strip()
    ]
    if missing:
        return _json(
            {
                "errorStatus": 1,
                "replyMessage": f"Missing required field(s): {', '.join(missing)}",
                "caseId": "",
            }
        )
    licence = str(body["driverLicenceNumber"])
    open_case = None
    for group in _documents_by_group(_pdfs(_service())):
        if _same_licence(_licence(group), licence):
            case = _case(group)
            open_case = {
                "caseId": case["case_id"],
                "caseTitle": case["case_title"],
                "caseType": "Suspected Medical Condition",
                "dmerStatus": "Active",
            }
            break
    return _record_write(
        req,
        req.route_params.get("guid", ""),
        "add-driver",
        {
            "resultMessage": "Driver successfully linked to document",
            "driverOpenCase": open_case,
        },
    )


@app.route(
    route="mercury/dmers/{guid}/add_case",
    methods=["POST"],
    auth_level=func.AuthLevel.ANONYMOUS,
)
def mercury_add_case(req: func.HttpRequest) -> func.HttpResponse:
    """Mercury's POST dmers/{documentRecordId}/add_case: give the DMER a case.
    Recorded under <source>/add-case/."""
    guid = req.route_params.get("guid", "")
    try:
        body = req.get_json()
    except ValueError:
        return _problem(400, "body must be JSON")
    if not isinstance(body, dict):
        return _problem(400, "body must be a JSON object")
    missing = [k for k in ("caseType", "documentType", "driver") if not body.get(k)]
    if missing:
        return _problem(400, "missing required field(s): " + ", ".join(missing))
    driver = body.get("driver")
    licence = str((driver if isinstance(driver, dict) else {}).get("driverId") or "")
    groups = _documents_by_group(_pdfs(_service()))
    group = next((g for g in groups if _same_licence(_licence(g), licence)), None)
    if not licence or group is None:
        return _problem(404, "driver not found")
    source = _source_blob(guid)
    if source is None or _driver_group(source) != group:
        return _problem(409, "the DMER relates to a different driver")
    if _has_case(group):
        return _problem(409, "the DMER already has a case")
    return _record_write(
        req,
        guid,
        "add-case",
        {
            "caseId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"mock-case/{guid}")),
            "documentRecordId": guid,
            "status": "Created",
        },
    )
