"""Anti-corruption layer for the Mercury (Dynamics) system.

Wraps the batch/backlog GET API used by the Page Poller and Webhook Listener
(see ``docs/development/stages/01-ingest.md``) and the ``GET by driver_licence``
lookup used by Driver Lookup (``03-document-orchestration.md``): cursor-based pagination
(question M-2, confirmed), Bearer-token auth from a Key Vault reference
(Mercury is outside our tenant boundary, over ExpressRoute -- key/credential
auth, not Managed Identity, per question M-4), and retry/circuit-breaker
wrapping, matching every other external client in this package.

It also wraps the **write-back** the Outbox Publisher needs
(:meth:`MercuryClient.send_outbox`, one call per ``mercury_outbox`` row).
**The write endpoints and bodies are provisional**: Mercury has not shared
its POST/PUT structure or idempotency behaviour yet (question M-6). The shape
here is what the dev mock implements; when M-6 is answered only
:meth:`MercuryClient.send_outbox` and the mock should need to change. Every
write sends the row's idempotency key as the ``Idempotency-Key`` header.

Case/webhook payload parsing is not implemented here yet. Extend this module
rather than adding a second Mercury client.

Each API's settings are read when that API is first used, so a service that
only calls one of them needs only that one's configuration.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from ..config import (
    MercuryDriverSettings,
    MercurySettings,
    MercuryWriteSettings,
    mercury_driver_settings,
    mercury_settings,
    mercury_write_settings,
)
from ..retry import CircuitBreaker, mercury_breaker, mercury_retry
from ..retry.circuit_breaker import CircuitOpenError
from ..telemetry import get_logger

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
    """One page of the Mercury batch GET response.

    ``next_url`` is the full URL to fetch the next page (Mercury's own
    ``nextLink``, followed as-is rather than re-derived from an opaque
    cursor value), or ``None`` when this is the last page.
    """

    records: list[dict[str, Any]]
    next_url: str | None


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
    ) -> None:
        self._batch_settings = settings
        self._driver_settings = driver_settings
        self._write_settings = write_settings
        self._http_get = http_get
        self._http_send = http_send
        self._breaker = breaker or mercury_breaker()

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

    def get_page(
        self, *, queue: str, page_size: int = 50, next_url: str | None = None
    ) -> MercuryPage:
        """Fetch one page of the backlog.

        ``next_url`` (from a prior :class:`MercuryPage`), when given, is
        fetched directly -- the Page Poller should not reconstruct the query
        string itself once pagination has started. Retried with backoff on
        transient failure (Requirement per 01-ingest.md's "Failure handling");
        the caller's own poll_checkpoint is what makes a retry-from-scratch
        safe.
        """
        url = next_url or (
            f"{self._settings.base_url}?queue={queue}&page_size={page_size}"
        )

        @mercury_retry()
        def _run() -> MercuryPage:
            return self._fetch(url)

        return self._breaker.call(_run)

    def _fetch(self, url: str) -> MercuryPage:
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Accept": "application/json",
        }
        status, body = self._http_get(url, headers=headers)
        if status < 200 or status >= 300:
            _log.error(
                "mercury batch API call failed",
                extra={"status": status, "url": url},
            )
            raise MercuryApiError(f"Mercury batch API returned HTTP {status}")
        payload = json.loads(body.decode("utf-8"))
        records = payload.get("value", [])
        _log.info(
            "mercury batch API page fetched",
            extra={
                "record_count": len(records),
                "has_next": bool(payload.get("nextLink")),
            },
        )
        return MercuryPage(records=records, next_url=payload.get("nextLink"))

    def get_driver_by_licence(self, licence_number: str) -> list[dict[str, Any]]:
        """Every driver Mercury holds under *licence_number* (``GET by
        driver_licence``): ``[]`` when there is none (HTTP 404), one driver
        normally, more if Mercury returns a list of several.

        The licence is part of the URL, so neither the URL nor the licence is
        ever logged (architecture doc §9.2). Retried with backoff on
        transient failure, behind the Mercury circuit breaker.
        """
        settings = self.driver_settings
        url = f"{settings.base_url}/{quote(licence_number, safe='')}"
        headers = {
            "Authorization": f"Bearer {settings.api_key}",
            "Accept": "application/json",
        }

        @mercury_retry()
        def _run() -> list[dict[str, Any]]:
            status, body = self._http_get(url, headers=headers)
            if status == 404:
                return []
            if status < 200 or status >= 300:
                _log.error("mercury driver lookup failed", extra={"status": status})
                raise MercuryApiError(f"Mercury driver lookup returned HTTP {status}")
            payload = json.loads(body.decode("utf-8"))
            drivers = payload if isinstance(payload, list) else [payload]
            if not all(isinstance(d, dict) for d in drivers):
                raise MercuryApiError(
                    "Mercury driver lookup returned an unexpected shape"
                )
            _log.info("mercury driver lookup", extra={"driver_count": len(drivers)})
            return drivers

        return self._breaker.call(_run)

    # -- write-back (Outbox Publisher) --------------------------------------

    def send_outbox(
        self, operation: str, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """Apply one ``mercury_outbox`` row to Mercury; returns Mercury's
        response (stored as ``mercury_response``).

        Provisional endpoints (M-6), relative to ``MERCURY_WRITE_API_BASE_URL``:

        - ``UPDATE_OUTCOME`` -- ``PUT documents/{guid}/outcome``;
        - ``MARK_DUPLICATE`` -- ``PUT documents/{guid}/status`` (Rejected,
          detached from the case, I-7);
        - ``MAP_DRIVER`` -- ``PUT documents/{guid}/driver`` (I-11);
        - ``CREATE_CASE`` -- live check ``GET drivers/{id}/cases?status=open``,
          then ``PUT documents/{guid}/case`` to attach to the open case, or
          ``POST cases`` when there is none (I-13).

        Raises :class:`MercuryWriteError` (``retryable`` set). No retry here:
        the publisher backs off per row. The payload carries clinical content
        and the URL a document id -- neither is logged.
        """
        guid = quote(str(payload["document_guid"]), safe="")
        body = {k: v for k, v in payload.items() if k != "document_guid"}
        if operation == "UPDATE_OUTCOME":
            return self._write(
                "PUT", f"documents/{guid}/outcome", body, idempotency_key
            )
        if operation == "MARK_DUPLICATE":
            return self._write(
                "PUT",
                f"documents/{guid}/status",
                {**body, "detach_from_case": True},
                idempotency_key,
            )
        if operation == "MAP_DRIVER":
            return self._write("PUT", f"documents/{guid}/driver", body, idempotency_key)
        if operation == "CREATE_CASE":
            return self._ensure_case(
                guid, str(payload["document_guid"]), body, idempotency_key
            )
        raise MercuryWriteError(
            f"unknown outbox operation {operation!r}", status=None, retryable=False
        )

    def _ensure_case(
        self, guid: str, document_guid: str, body: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        driver_id = body.get("mercury_driver_id")
        if not driver_id:
            raise MercuryWriteError(
                "CREATE_CASE needs the driver's Mercury id",
                status=None,
                retryable=False,
            )
        cases = self._write(
            "GET",
            f"drivers/{quote(str(driver_id), safe='')}/cases?status=open",
            None,
            None,
        )
        open_cases = cases.get("value", []) if isinstance(cases, dict) else cases
        if open_cases:
            case_id = open_cases[0]["case_id"]
            response = self._write(
                "PUT",
                f"documents/{guid}/case",
                {"case_id": case_id, "comment": body.get("comment")},
                idempotency_key,
            )
            return {"action": "attached", "case_id": case_id, "response": response}
        response = self._write(
            "POST", "cases", {**body, "document_guid": document_guid}, idempotency_key
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
            "Authorization": f"Bearer {settings.api_key}",
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
                status, raw = self._http_send(method, url, headers=headers, body=data)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                raise MercuryWriteError(
                    f"Mercury write {method} failed: {type(exc).__name__}",
                    status=None,
                    retryable=True,
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
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            return {"raw": raw.decode("utf-8", errors="replace")[:1000]}
