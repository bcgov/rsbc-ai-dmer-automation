"""Anti-corruption layer for the Mercury (Dynamics) system.

Wraps the batch/backlog GET API used by the Page Poller and Webhook Listener
(see ``docs/development/stages/01-ingest.md``): cursor-based pagination
(question M-2, confirmed), Bearer-token auth from a Key Vault reference
(Mercury is outside our tenant boundary, over ExpressRoute -- key/credential
auth, not Managed Identity, per question M-4), and retry/circuit-breaker
wrapping, matching every other external client in this package.

Case/webhook payload parsing and Mercury write-back (POST/PUT for outcomes,
case creation) are not implemented here yet -- this covers only the read
path Ingest needs. Extend this module rather than adding a second Mercury
client when that work starts.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from ..config import MercurySettings, mercury_settings
from ..retry import CircuitBreaker, mercury_breaker, mercury_retry
from ..telemetry import get_logger

_log = get_logger(__name__)


class MercuryApiError(RuntimeError):
    """Raised when the Mercury batch API returns a non-2xx response."""


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
        Optional connection settings; defaults to
        :func:`dmer_common.config.mercury_settings`.
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
        http_get: _HttpGet = _default_http_get,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings or mercury_settings()
        self._http_get = http_get
        self._breaker = breaker or mercury_breaker()

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
