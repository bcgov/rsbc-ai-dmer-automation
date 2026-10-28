"""Document Intelligence client wrapper.

Provides ``analyze(...) -> DIResult`` against a DI model (custom-trained model for
top-level fields, or ``prebuilt-read`` for tiled OCR), authenticated via Managed
Identity over the private endpoint, and wrapped by retry + circuit breaker.

The underlying SDK client is injectable so unit tests can supply a mock without a
network call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..retry import CircuitBreaker, di_breaker, di_retry
from ..telemetry import get_logger

_log = get_logger(__name__)


@dataclass(frozen=True)
class DIResult:
    """Normalized Document Intelligence analysis result.

    ``content`` is the concatenated text; ``pages`` and ``documents`` hold the
    structured page/field data as plain dicts so callers do not depend on the
    raw SDK object graph.
    """

    content: str
    pages: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    tables: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_sdk(cls, result: Any) -> DIResult:
        """Adapt an ``AnalyzeResult`` (or mapping) into a :class:`DIResult`."""
        as_dict = result.as_dict() if hasattr(result, "as_dict") else dict(result)
        return cls(
            content=as_dict.get("content", "") or "",
            pages=list(as_dict.get("pages", []) or []),
            documents=list(as_dict.get("documents", []) or []),
            tables=list(as_dict.get("tables", []) or []),
        )


class DocumentIntelligenceClient:
    """Managed-Identity DI client wrapper with retry + circuit breaker.

    Parameters
    ----------
    endpoint:
        The DI resource endpoint (private endpoint URL).
    credential:
        Optional credential; defaults to ``DefaultAzureCredential`` (Managed
        Identity). Injectable for tests.
    sdk_client:
        Optional pre-built SDK client (used by tests to inject a mock). When
        provided, ``endpoint``/``credential`` are ignored.
    breaker:
        Optional shared circuit breaker (defaults to a DI-tuned breaker).
    """

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        credential: Any | None = None,
        sdk_client: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._breaker = breaker or di_breaker()
        if sdk_client is not None:
            self._client = sdk_client
        else:
            if not endpoint:
                raise ValueError("endpoint is required when sdk_client is not provided")
            from azure.ai.documentintelligence import (
                DocumentIntelligenceClient as _SdkClient,
            )
            from azure.identity import DefaultAzureCredential

            # Strip trailing slashes so the SDK does not produce a double slash
            # (e.g. ".../project//documentintelligence/...") when it appends paths.
            self._client = _SdkClient(
                endpoint=endpoint.rstrip("/"),
                credential=credential or DefaultAzureCredential(),
            )

    def analyze(
        self, model_id: str, document: bytes, *, pages: str | None = None
    ) -> DIResult:
        """Analyze ``document`` bytes with ``model_id`` and return a DIResult.

        The call is retried with backoff and guarded by the circuit breaker so a
        sustained DI outage fails fast (Requirements 4.1, 4.2, 5.2, 4.5).
        """

        @di_retry()
        def _run() -> DIResult:
            from azure.ai.documentintelligence.models import AnalyzeDocumentRequest

            kwargs: dict[str, Any] = {
                "model_id": model_id,
                "body": AnalyzeDocumentRequest(bytes_source=document),
            }
            if pages is not None:
                kwargs["pages"] = pages
            poller = self._client.begin_analyze_document(**kwargs)
            return DIResult.from_sdk(poller.result())

        _log.info("analyzing document", extra={"model_id": model_id, "pages": pages})
        return self._breaker.call(_run)
