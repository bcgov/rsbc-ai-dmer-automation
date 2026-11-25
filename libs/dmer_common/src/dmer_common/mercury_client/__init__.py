"""Anti-corruption layer for the Mercury (Dynamics) system.

Wraps the ``GET dmers`` API used by the Page Poller (see
``docs/development/stages/01-ingest.md``), the AI Processed status call that
claims what it read, the file download the Ingest Function uses
(``GET {base}/{documentRecordId}/files``), and the ``POST drivers/lookup``
lookup (``POST drivers/lookup``) used by Driver Lookup
(``03-document-orchestration.md``). ``GET dmers``
is claim-based: each call returns the next records not yet marked AI
Processed, so paging is "mark, then call again"
(:meth:`MercuryClient.set_ai_processed`), not a cursor. Mercury's camelCase
items are translated here into the snake_case records the rest of the
pipeline reads.

Every call carries a bearer token: the key (a Key Vault reference) buys one
from ``Authentication/token``, and it is reused until it expires or Mercury
answers 401 (:mod:`.auth`). Mercury is outside our tenant boundary, over
ExpressRoute -- key/credential auth, not Managed Identity, per question M-4.
Calls are wrapped in retry and a circuit breaker, matching every other
external client in this package.

It also wraps the **write-back** the Outbox Publisher needs
(:meth:`MercuryClient.send_outbox`, one call per ``mercury_outbox`` row).
The outcome goes through Mercury's ``SetDmerAIProccessedStatus``
(:mod:`.ai_status`), the driver mapping through ``add_driver``
(:mod:`.driver_link`) and the case through ``add_case`` (:mod:`.case_link`). Every write sends the row's
idempotency key as the ``Idempotency-Key`` header.

Case/webhook payload parsing is not implemented here yet. Extend this module
rather than adding a second Mercury client.

Each API's settings are read when that API is first used, so a service that
only calls one of them needs only that one's configuration.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote
from zoneinfo import ZoneInfo

from ..config import (
    MercuryAuthSettings,
    MercuryDriverSettings,
    MercurySettings,
    MercuryWriteSettings,
    mercury_auth_settings,
    mercury_driver_settings,
    mercury_settings,
    mercury_write_settings,
)
from ..retry import CircuitBreaker, mercury_breaker, mercury_retry
from ..retry.circuit_breaker import CircuitOpenError
from ..telemetry import get_logger
from .ai_status import AI_STATUS_PATH, UnknownOutcomeError, ai_status_request
from .auth import MercuryAuth, MercuryAuthError, shared_auth
from .case_link import (
    ADD_CASE_PATH,
    DRIVER_LOOKUP_PATH,
    add_case_request,
    is_placeholder_case,
    open_case_id,
)
from .driver_link import ADD_DRIVER_PATH, add_driver_request

_log = get_logger(__name__)


class MercuryApiError(RuntimeError):
    """Raised when the Mercury batch API returns a non-2xx response."""


class MercuryWriteError(MercuryApiError):
    """A Mercury write failed. ``retryable``: the same write may succeed later
    (network error, timeout, 408, 429, 5xx, or the circuit breaker open);
    otherwise Mercury rejected it (any other 4xx) and retrying won't help."""

    def __init__(self, message: str, *, status: int | None, retryable: bool) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class _HttpSend(Protocol):
    """An HTTP call with a JSON body -- injectable for tests."""

    def __call__(
        self, method: str, url: str, *, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]: ...


def _default_http_send(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _retryable(status: int) -> bool:
    return status in (408, 429) or status >= 500


class _HttpGet(Protocol):
    """Subset of an HTTP GET call this client needs -- injectable for tests."""

    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]: ...


def _default_http_get(url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
    """Perform a GET via stdlib ``urllib`` (no new HTTP dependency -- matches
    the pattern already used by ``services/intake-processor/function_app.py``).
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


@dataclass(frozen=True)
class MercuryPage:
    """One page of ``GET dmers``: every item Mercury returned (DMER or not),
    as snake_case records (:func:`_record`).

    ``has_more``: the page came back **full** (as many items as asked for),
    so more may be waiting. Mercury returns no ``nextLink`` (confirmed
    2026-11-24); the next page is simply the same request once these are
    marked AI Processed. ``count`` is Mercury's ``count`` as sent: the page
    size asked for, echoed back (Mercury DEV, 2026-11-24) -- only logged.
    """

    records: list[dict[str, Any]]
    has_more: bool
    count: int | None = None


# Mercury's endpoint name, spelling included.
_AI_PROCESSED_PATH = "BatchSetDmerAIProccessedStatus"


def _has_driver_details(payload: dict[str, Any]) -> bool:
    return all(
        str(payload.get(k) or "").strip()
        for k in ("licence_number", "first_name", "last_name")
    )


_DRIVER_LOOKUP_PATH = "lookup"


def _first(item: dict[str, Any], *keys: str) -> Any:
    """The first of *keys* present in *item* (camelCase from Mercury, or the
    pipeline's snake_case), with "" read as None."""
    for key in keys:
        if key in item:
            return _value(item[key])
    return None


def _lookup_document(doc: dict[str, Any]) -> dict[str, Any]:
    """One of the driver's documents. Mercury's ``document_id`` is its
    document record id -- the pipeline's ``document_guid``, not our own
    ``document_id``. Mercury gives no ``document_url`` here (the file is
    fetched by id), and ``dps_date`` only once the document is triaged."""
    return {
        "document_guid": _first(
            doc, "document_id", "documentId", "documentRecordId", "document_guid"
        ),
        "document_name": _first(
            doc, "file_name", "fileName", "documentName", "document_name"
        ),
        "document_type": _first(doc, "documentType", "document_type"),
        "document_status": _first(
            doc, "submittalStatus", "documentStatus", "document_status"
        ),
        "uploaded_date": _first(doc, "uploadedDate", "uploaded_date"),
        "dps_date": _first(doc, "dpsDate", "dps_date"),
        "document_url": _first(doc, "documentUrl", "document_url"),
    }


def _lookup_driver(driver: dict[str, Any]) -> dict[str, Any]:
    """One driver from ``drivers/lookup`` in the pipeline's shape.

    Mercury answers with the driver's fields in snake_case, ``documents`` in
    camelCase and the driver's open case as ``activeCase`` (Mercury DEV's
    Swagger, 2026-11-24). The camelCase of ``GET dmers``' ``driver`` object
    and the older ``case`` are read as well."""
    documents = _first(driver, "documents", "activeDocuments", "active_documents")
    case = _first(driver, "activeCase", "case", "dmerDocumentCaseRecord")
    return {
        "driver_id": _first(driver, "driverId", "driver_id"),
        "first_name": _first(driver, "firstName", "first_name"),
        "middle_name": _first(driver, "middleName", "middle_name"),
        "last_name": _first(driver, "lastName", "last_name"),
        "licence_number": _first(driver, "licenceNumber", "licence_number"),
        "active_documents": [
            _lookup_document(d) for d in documents or [] if isinstance(d, dict)
        ],
        "case": case if isinstance(case, dict) else None,
    }


_RECEIVED_DATE_FORMAT = "%m/%d/%Y %H:%M:%S"  # e.g. "05/06/2025 10:03:01"


def _value(value: Any) -> Any:
    """Mercury sends "" for an unset field; the pipeline uses None."""
    return None if value == "" else value


def _received_date(value: Any, zone: ZoneInfo) -> str | None:
    """``receivedDate`` as ISO-8601 with an offset. Mercury sends
    "MM/DD/YYYY HH:MM:SS" with no zone (read in *zone*); an ISO-8601 value is
    also accepted, and keeps its own offset if it has one."""
    if not value:
        return None
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, _RECEIVED_DATE_FORMAT).replace(tzinfo=zone)
        except ValueError:
            # The value itself is not logged: it is document data.
            _log.warning("mercury receivedDate not understood; left unset")
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.isoformat()


def _record(item: dict[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    """One ``GET dmers`` item as the record the Page Poller stores.
    ``documentRecordId`` is the document guid -- also the id the AI Processed
    call takes. ``submittalStatus`` is kept as the document status and
    ``dmerStatus`` beside it, until Mercury says which one we should read."""
    driver = item.get("driver")
    case = item.get("dmerDocumentCaseRecord")
    return {
        "document_guid": _value(item.get("documentRecordId")),
        "document_name": _value(item.get("documentName")),
        "document_url": _value(item.get("documentUrl")),
        "document_type": _value(item.get("documentType")),
        "document_status": _value(item.get("submittalStatus")),
        "dmer_status": _value(item.get("dmerStatus")),
        "document_priority": _value(item.get("documentPriority")),
        "received_date": _received_date(item.get("receivedDate"), zone),
        "document_type_business_area": _value(item.get("documentTypeBusinessArea")),
        "queue": _value(item.get("queue")),
        "driver": (
            {
                "driver_id": _value(driver.get("driverId")),
                "first_name": _value(driver.get("firstName")),
                "middle_name": _value(driver.get("middleName")),
                "last_name": _value(driver.get("lastName")),
                "licence_number": _value(driver.get("licenceNumber")),
                "documents": driver.get("documents") or [],
            }
            if isinstance(driver, dict)
            else None
        ),
        "case": (
            {
                "case_id": _value(case.get("caseId")),
                "case_title": _value(case.get("caseTitle")),
                "case_type": _value(case.get("caseType")),
                "case_priority": _value(case.get("casePriority")),
                "case_owner": _value(case.get("caseOwner")),
                "case_status": _value(case.get("caseStatus")),
            }
            if isinstance(case, dict)
            else None
        ),
    }


class MercuryClient:
    """Batch GET client for Mercury's DMER backlog API.

    Parameters
    ----------
    settings:
        Optional batch API settings; defaults to
        :func:`dmer_common.config.mercury_settings`, read on first use.
    driver_settings:
        Optional driver-licence lookup settings; defaults to
        :func:`dmer_common.config.mercury_driver_settings`, read on first use.
    http_get:
        Optional injectable GET function (used by tests to fake the network
        call without hitting a real endpoint).
    breaker:
        Optional shared circuit breaker (defaults to a Mercury-tuned breaker).
    auth:
        Optional bearer-token provider; defaults to the process-wide one for
        :func:`dmer_common.config.mercury_auth_settings`, read on first use.
    """

    def __init__(
        self,
        *,
        settings: MercurySettings | None = None,
        driver_settings: MercuryDriverSettings | None = None,
        write_settings: MercuryWriteSettings | None = None,
        http_get: _HttpGet = _default_http_get,
        http_send: _HttpSend = _default_http_send,
        breaker: CircuitBreaker | None = None,
        auth: MercuryAuth | None = None,
        auth_settings: MercuryAuthSettings | None = None,
    ) -> None:
        self._batch_settings = settings
        self._driver_settings = driver_settings
        self._write_settings = write_settings
        self._http_get = http_get
        self._http_send = http_send
        self._breaker = breaker or mercury_breaker()
        self._auth_provider = auth
        self._auth_settings = auth_settings

    @property
    def _auth(self) -> MercuryAuth:
        if self._auth_provider is None:
            self._auth_provider = shared_auth(
                self._auth_settings or mercury_auth_settings(),
                http_get=self._http_get,
            )
        return self._auth_provider

    def _get(self, url: str, headers: dict[str, str]) -> tuple[int, bytes]:
        """GET with the bearer token; on a 401 the token is dropped and the
        call made once more with a new one."""
        return self._authorised(lambda h: self._http_get(url, headers=h), headers)

    def _send(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        """As :meth:`_get`, for a call with a body."""
        return self._authorised(
            lambda h: self._http_send(method, url, headers=h, body=body), headers
        )

    def _authorised(
        self,
        call: Callable[[dict[str, str]], tuple[int, bytes]],
        headers: dict[str, str],
    ) -> tuple[int, bytes]:
        token = self._auth.token()
        status, body = call({**headers, "Authorization": f"Bearer {token}"})
        if status != 401:
            return status, body
        # The token expired early or was revoked: fetch a new one, once.
        _log.info("mercury rejected the bearer token; fetching a new one")
        self._auth.invalidate(token)
        token = self._auth.token()
        return call({**headers, "Authorization": f"Bearer {token}"})

    @property
    def _settings(self) -> MercurySettings:
        if self._batch_settings is None:
            self._batch_settings = mercury_settings()
        return self._batch_settings

    @property
    def write_settings(self) -> MercuryWriteSettings:
        if self._write_settings is None:
            self._write_settings = mercury_write_settings()
        return self._write_settings

    @property
    def driver_settings(self) -> MercuryDriverSettings:
        if self._driver_settings is None:
            self._driver_settings = mercury_driver_settings()
        return self._driver_settings

    def get_page(self, *, page_size: int = 50) -> MercuryPage:
        """``GET dmers``: the next *page_size* records not yet marked AI
        Processed -- the same ones again until they are
        (:meth:`set_ai_processed`). Retried with backoff on transient failure
        (01-ingest.md's "Failure handling").

        The page size is Mercury's ``count`` query parameter (Mercury DEV's
        Swagger, 2026-11-24; ``pageSize`` is ignored).
        """
        url = f"{self._settings.base_url}?count={page_size}"

        @mercury_retry()
        def _run() -> MercuryPage:
            return self._fetch(url, page_size)

        return self._breaker.call(_run)

    def _fetch(self, url: str, page_size: int) -> MercuryPage:
        headers = {
            "Accept": "application/json",
        }
        status, body = self._get(url, headers)
        if status < 200 or status >= 300:
            _log.error(
                "mercury batch API call failed",
                extra={"status": status, "url": url},
            )
            raise MercuryApiError(f"Mercury batch API returned HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
        zone = ZoneInfo(self._settings.received_date_timezone)
        items = payload.get("items") or []
        records = [_record(item, zone) for item in items if isinstance(item, dict)]
        raw_count = payload.get("count")
        count = raw_count if isinstance(raw_count, int) else None
        has_more = len(items) >= page_size
        _log.info(
            "mercury batch API page fetched",
            extra={
                "record_count": len(records),
                "page_size": page_size,
                "mercury_count": count,
                "has_more": has_more,
            },
        )
        return MercuryPage(records=records, has_more=has_more, count=count)

    def download_dmer(self, document_guid: str) -> bytes:
        """The DMER's PDF (``GET {base}/{documentRecordId}/files``), as bytes.

        Mercury's ``documentUrl`` is a path inside Mercury, not a link we can
        fetch; the file is downloaded by the document guid instead, with the
        batch API's base URL and key. Retried with backoff, behind the Mercury
        circuit breaker; raises :class:`MercuryApiError` on a non-2xx response
        or when the body is not a PDF. Neither the URL (it carries the guid)
        nor the content is logged.
        """
        url = f"{self._settings.base_url}/{quote(document_guid, safe='')}/files"
        headers = {
            "Accept": "application/pdf, application/octet-stream",
        }

        @mercury_retry()
        def _run() -> bytes:
            status, body = self._get(url, headers)
            if status < 200 or status >= 300:
                _log.error("mercury file download failed", extra={"status": status})
                raise MercuryApiError(f"Mercury file download returned HTTP {status}")
            if not body.lstrip()[:5] == b"%PDF-":
                raise MercuryApiError("Mercury file download did not return a PDF")
            return body

        content = self._breaker.call(_run)
        _log.info("mercury file downloaded", extra={"size_bytes": len(content)})
        return content

    def set_ai_processed(self, document_guids: list[str]) -> None:
        """Mark *document_guids* AI Processed in Mercury
        (``POST {base}/BatchSetDmerAIProccessedStatus``, ``{"dmerIds": [...]}``):
        Mercury moves them to the AI Processed Queue, out of the DPS queues,
        and ``GET dmers`` stops returning them. Retried with backoff on
        failure; raises :class:`MercuryApiError` once retries are exhausted.
        The ids are not logged, only how many.
        """
        if not document_guids:
            return
        url = f"{self._settings.base_url}/{_AI_PROCESSED_PATH}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        body = json.dumps({"dmerIds": list(document_guids)}).encode("utf-8")

        @mercury_retry()
        def _run() -> None:
            status, _ = self._send("POST", url, headers, body)
            if status < 200 or status >= 300:
                _log.error(
                    "mercury AI processed status call failed", extra={"status": status}
                )
                raise MercuryApiError(
                    f"Mercury AI processed status call returned HTTP {status}"
                )

        self._breaker.call(_run)
        _log.info(
            "mercury AI processed status set",
            extra={"record_count": len(document_guids)},
        )

    def get_driver_by_licence(self, licence_number: str) -> list[dict[str, Any]]:
        """Every driver Mercury holds under *licence_number*
        (``POST {base}/lookup`` with ``{"driverLicenceNumber": ...}``): ``[]``
        when there is none (HTTP 404, or an empty answer), one driver
        normally, more if Mercury returns a list of several. Each driver is
        translated to the pipeline's shape (:func:`_lookup_driver`).

        The licence travels in the body, and neither it nor the response is
        logged (architecture doc §9.2). Retried with backoff on transient
        failure, behind the Mercury circuit breaker.
        """
        url = f"{self.driver_settings.base_url}/{_DRIVER_LOOKUP_PATH}"
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        body = json.dumps({"driverLicenceNumber": licence_number}).encode("utf-8")

        @mercury_retry()
        def _run() -> list[dict[str, Any]]:
            status, raw = self._send("POST", url, headers, body)
            if status == 404:
                return []
            if status < 200 or status >= 300:
                _log.error("mercury driver lookup failed", extra={"status": status})
                raise MercuryApiError(f"Mercury driver lookup returned HTTP {status}")
            payload = json.loads(raw.decode("utf-8")) if raw.strip() else None
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                payload = payload["items"]
            if payload is None or payload == {}:
                payload = []
            drivers = payload if isinstance(payload, list) else [payload]
            if not all(isinstance(d, dict) for d in drivers):
                raise MercuryApiError(
                    "Mercury driver lookup returned an unexpected shape"
                )
            _log.info("mercury driver lookup", extra={"driver_count": len(drivers)})
            return [_lookup_driver(d) for d in drivers]

        return self._breaker.call(_run)

    # -- write-back (Outbox Publisher) --------------------------------------

    def send_outbox(
        self, operation: str, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Apply one ``mercury_outbox`` row to Mercury; returns Mercury's
        response (stored as ``mercury_response``).

        Endpoints, relative to ``MERCURY_WRITE_API_BASE_URL``:

        - ``UPDATE_OUTCOME`` -- ``POST Dmers/{guid}/setDmerAIProccessedStatus``
          (Mercury's API, :mod:`.ai_status`): the outcome's priority, queue
          and status with the comment as notes; a fallback (``decided_by``
          FALLBACK) as a processing error; a duplicate as the duplicate flag
          only;
        - ``MARK_DUPLICATE`` -- the same call, duplicate flag only. No longer
          queued (a duplicate's ``UPDATE_OUTCOME`` carries ``is_duplicate``);
          kept for rows queued before that change.

        - ``MAP_DRIVER`` -- ``POST dmers/{guid}/add_driver`` (Mercury's API,
          I-11): links the driver matched from the licence to the DMER;
          Mercury answers with the driver's open case, if any.

        - ``CREATE_CASE`` -- (I-13) skipped when the DMER already has a case;
          otherwise a live ``POST drivers/lookup`` by licence, skipped when
          the driver has an open case; otherwise ``POST
          dmers/{guid}/add_case`` (Mercury's API, :mod:`.case_link`).

        Raises :class:`MercuryWriteError` (``retryable`` set). No retry here:
        the publisher backs off per row. The payload carries clinical content
        and the URL a document id -- neither is logged.
        """
        guid = quote(str(payload["document_guid"]), safe="")
        if operation in ("UPDATE_OUTCOME", "MARK_DUPLICATE"):
            try:
                request = ai_status_request(
                    outcome_code=payload.get("outcome_code"),
                    notes=payload.get("comment"),
                    processing_error=payload.get("decided_by") == "FALLBACK",
                    is_duplicate=(
                        operation == "MARK_DUPLICATE"
                        or bool(payload.get("is_duplicate"))
                    ),
                    duplicate_of_document_guid=payload.get(
                        "duplicate_of_document_guid"
                    ),
                    driver_link_error=payload.get("driver_link_error"),
                    case_error=payload.get("case_error"),
                )
            except UnknownOutcomeError as exc:
                raise MercuryWriteError(str(exc), status=None, retryable=False) from exc
            return self._write(
                "POST", AI_STATUS_PATH.format(guid=guid), request, idempotency_key
            )
        if operation == "MAP_DRIVER" and not _has_driver_details(payload):
            raise MercuryWriteError(
                "MAP_DRIVER needs the driver's licence, first name and surname",
                status=None,
                retryable=False,
            )
        if operation == "MAP_DRIVER":
            return self._write(
                "POST",
                ADD_DRIVER_PATH.format(guid=guid),
                add_driver_request(payload),
                idempotency_key,
            )
        if operation == "CREATE_CASE":
            return self._ensure_case(guid, payload, idempotency_key)
        raise MercuryWriteError(
            f"unknown outbox operation {operation!r}", status=None, retryable=False
        )

    def _ensure_case(
        self, guid: str, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """``CREATE_CASE``: give the DMER a case when it needs one.

        After the AI linked the driver (``driver_linked``, from ``add_driver``):
        create one unless ``add_driver`` reported a real active case (the
        placeholder driver's case doesn't count). Otherwise:
        only when the DMER has no case and its driver no open case (live
        lookup). When nothing is needed, nothing is sent."""
        if payload.get("driver_linked"):
            open_case = payload.get("driver_open_case")
            if open_case and not is_placeholder_case(open_case):
                return {
                    "action": "skipped",
                    "reason": "driver_has_open_case",
                    "case_id": open_case.get("caseId") or open_case.get("case_id"),
                }
            return self._add_case(guid, payload, idempotency_key)
        if payload.get("mercury_case_id"):
            return {
                "action": "skipped",
                "reason": "dmer_has_case",
                "case_id": payload["mercury_case_id"],
            }
        licence = str(payload.get("licence_number") or "").strip()
        if not licence:
            raise MercuryWriteError(
                "CREATE_CASE needs the driver's licence number",
                status=None,
                retryable=False,
            )
        lookup = self._write(
            "POST", DRIVER_LOOKUP_PATH, {"driverLicenceNumber": licence}, None
        )
        case_id = open_case_id(lookup)
        if case_id:
            return {
                "action": "skipped",
                "reason": "driver_has_open_case",
                "case_id": case_id,
            }
        return self._add_case(guid, payload, idempotency_key)

    def _add_case(
        self, guid: str, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        licence = str(payload.get("licence_number") or "").strip()
        if not licence:
            raise MercuryWriteError(
                "CREATE_CASE needs the driver's licence number",
                status=None,
                retryable=False,
            )
        response = self._write(
            "POST",
            ADD_CASE_PATH.format(guid=guid),
            add_case_request(licence),
            idempotency_key,
        )
        return {"action": "created", "response": response}

    def _write(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        settings = self.write_settings
        url = f"{settings.base_url}/{path}"
        headers = {
            "Accept": "application/json",
        }
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        def _run() -> tuple[int, bytes]:
            try:
                status, raw = self._send(method, url, headers, data)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise MercuryWriteError(
                    f"Mercury write {method} failed: {type(exc).__name__}",
                    status=None,
                    retryable=True,
                ) from exc
            except MercuryAuthError as exc:
                # No token: worth retrying only if Mercury was struggling.
                raise MercuryWriteError(
                    f"Mercury write {method} failed: no bearer token",
                    status=exc.status,
                    retryable=exc.status is not None and _retryable(exc.status),
                ) from exc
            if _retryable(status):
                # counted by the breaker: Mercury is struggling
                raise MercuryWriteError(
                    f"Mercury write {method} returned HTTP {status}",
                    status=status,
                    retryable=True,
                )
            return status, raw

        try:
            status, raw = self._breaker.call(_run)
        except CircuitOpenError as exc:
            raise MercuryWriteError(
                "Mercury circuit breaker open", status=None, retryable=True
            ) from exc
        if status < 200 or status >= 300:
            _log.error(
                "mercury write rejected", extra={"method": method, "status": status}
            )
            raise MercuryWriteError(
                f"Mercury write {method} returned HTTP {status}",
                status=status,
                retryable=False,
            )
        if not raw:
            return {}
        try:
            response = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"raw": raw.decode("utf-8", errors="replace")[:1000]}
        if isinstance(response, dict) and response.get("errorStatus") not in (
            None,
            0,
            "0",
        ):
            # Mercury's error shape ({"errorStatus", "replyMessage", ...}),
            # which may come with a 2xx. replyMessage is not logged (it may
            # repeat what was sent); it is kept on the row's last_error.
            _log.error(
                "mercury write rejected",
                extra={"method": method, "status": status, "error_status": True},
            )
            raise MercuryWriteError(
                f"Mercury write {method} rejected: "
                f"{str(response.get('replyMessage') or '')[:300]}",
                status=status,
                retryable=False,
            )
        return response
