"""The active procedures text: generated from the PDF once per PDF version.

The PDF is maintained in the ``rules`` container (beside ``rules.json``, and
read-only to the pipeline). Its text -- :func:`.policy_text.build`, converted
and verified -- is kept in the ``triage-policy`` container as
``<pdf sha256>.txt``, so it is generated once per PDF version, by whichever
worker first sees that version, and every other worker reads it.

Per document, :func:`load_policy` reads only the PDF's ETag: while it is
unchanged, this worker's copy of the text is used. A new ETag (the PDF was
replaced) downloads the PDF; its text is read from ``triage-policy`` if
another worker already generated it, and generated and stored otherwise.
One worker generates at most one text at a time (~15 s, ~25 MB).

Nothing here logs policy content -- only hashes, sizes and the date.
"""

from __future__ import annotations

import hashlib
import threading
from dataclasses import dataclass
from datetime import date

from azure.storage.blob import ContentSettings

from ..storage import BlobClient, rules, triage_policy, triage_policy_pdf_path
from ..telemetry import get_logger
from .policy_text import build, updated_date

_log = get_logger(__name__)

_TEXT_CONTENT = ContentSettings(content_type="text/plain; charset=utf-8")


class PolicyUnavailableError(RuntimeError):
    """The procedures PDF can't be read -- affects every document, not just
    one (like a missing ``rules.json``)."""


@dataclass(frozen=True)
class PolicyText:
    """The procedures text the triage LLM reads, and which PDF it came from."""

    text: str
    pdf_sha256: str
    pdf_etag: str
    updated: date | None  # the procedures' "Updated" date: the policy version


# This worker's text for the active PDF, by the PDF's ETag.
_cached: PolicyText | None = None
_lock = threading.Lock()


def text_path(pdf_sha256: str) -> str:
    """Path of a PDF version's text within the ``triage-policy`` container."""
    return f"{pdf_sha256}.txt"


def load_policy(blob: BlobClient) -> PolicyText:
    """The active procedures text. Raises :class:`PolicyUnavailableError` when
    the PDF can't be read, and :class:`.policy_text.PolicyTextError` when a
    new PDF can't be converted into a text that matches it."""
    global _cached
    pdf_url = blob.blob_url(rules(), triage_policy_pdf_path())
    try:
        etag = blob.etag(pdf_url)
    except Exception:  # noqa: BLE001 - any read failure: there is no usable PDF
        raise PolicyUnavailableError("Procedures PDF could not be read") from None
    if _cached is not None and _cached.pdf_etag == etag:
        return _cached
    with _lock:  # one conversion per worker; waiting callers reuse its result
        if _cached is not None and _cached.pdf_etag == etag:
            return _cached
        _cached = _load(blob, pdf_url, etag)
        return _cached


def _load(blob: BlobClient, pdf_url: str, etag: str) -> PolicyText:
    try:
        pdf = blob.download(pdf_url)
    except Exception:  # noqa: BLE001
        raise PolicyUnavailableError("Procedures PDF could not be read") from None
    sha = hashlib.sha256(pdf).hexdigest()
    container, path = triage_policy(), text_path(sha)
    stored = blob.download_if_exists(blob.blob_url(container, path))
    if stored is not None:
        text = stored.decode("utf-8")
        _log.info(
            "triage policy text loaded",
            extra={"pdf_sha256": sha, "text_chars": len(text)},
        )
    else:
        text = build(pdf)  # verified against the PDF, or raises
        # Workers that generate the same version at once write the same text.
        blob.upload_bytes(
            container, path, text.encode("utf-8"), content_settings=_TEXT_CONTENT
        )
        _log.info(
            "triage policy text generated",
            extra={"pdf_sha256": sha, "text_chars": len(text)},
        )
    return PolicyText(
        text=text, pdf_sha256=sha, pdf_etag=etag, updated=updated_date(text)
    )


def clear_cache() -> None:
    """Forget this worker's text (tests)."""
    global _cached
    _cached = None
