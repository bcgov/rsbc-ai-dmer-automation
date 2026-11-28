"""The driver's details as read from the DMER form, for the notes Mercury gets
when the pipeline can't find or link the driver.

Intake staff then see who the form is about without opening it: the licence,
first name and surname the form shows, read from the extraction's
``extracted-dmer/<document_id>/combined.json`` (``fields.dl_number``,
``fields.first_name``, ``fields.last_name``). Nothing new is stored: the
values are read when the note is written.

Used by:

- Driver Lookup's fallback (licence unreadable, driver not found, more than
  one driver) -- :func:`dmer_common.db.driver_lookup.unidentified_driver_comment`;
- the Outbox Publisher when ``add_driver`` fails for good -- the outcome's
  ``form_driver_note`` (:func:`dmer_common.mercury_client.ai_status.ai_status_request`).

Read best effort: a missing or unreadable ``combined.json`` leaves the line
out of the note; it never stops the note being sent.

PII: the values go only into the note sent to Mercury (and so into its outbox
row, like the rest of the note) -- never into logs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Final

from .licence import normalize_licence
from .storage.containers import extracted_dmer
from .storage.paths import combined_path
from .telemetry import get_logger

_log = get_logger(__name__)

FORM_DRIVER_NOTE_PREFIX: Final = "Driver details as read from the DMER form"
# combined.json field names (the custom model's).
LICENCE_FIELD: Final = "dl_number"
FIRST_NAME_FIELD: Final = "first_name"
LAST_NAME_FIELD: Final = "last_name"
# A misread can be a whole line of the form: keep the note readable.
_MAX_VALUE_LENGTH: Final = 100


@dataclass(frozen=True)
class FormDriver:
    """What the form says about the driver; None where it says nothing."""

    licence: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())[:_MAX_VALUE_LENGTH].strip()
    return text or None


def form_driver_from_combined(content: bytes | str | dict[str, Any]) -> FormDriver:
    """The driver's details from a ``combined.json`` (bytes, text or parsed).
    Raises ``ValueError`` when it isn't JSON."""
    data = (
        json.loads(content) if isinstance(content, (bytes, bytearray, str)) else content
    )
    fields = data.get("fields") if isinstance(data, dict) else None
    if not isinstance(fields, dict):
        return FormDriver()
    return FormDriver(
        licence=_clean(fields.get(LICENCE_FIELD)),
        first_name=_clean(fields.get(FIRST_NAME_FIELD)),
        last_name=_clean(fields.get(LAST_NAME_FIELD)),
    )


def read_form_driver(blob: Any, document_id: str) -> FormDriver:
    """The driver's details from the document's ``combined.json``, read with
    *blob* (a :class:`dmer_common.storage.BlobClient`). Best effort: an empty
    :class:`FormDriver` when the file is missing or can't be read."""
    try:
        content = blob.download_if_exists(
            blob.blob_url(extracted_dmer(), combined_path(document_id))
        )
        if content is None:
            _log.warning(
                "form driver details: combined.json not found",
                extra={"document_id": document_id},
            )
            return FormDriver()
        return form_driver_from_combined(content)
    except Exception as exc:  # noqa: BLE001 - the note goes without the details
        _log.warning(
            "form driver details: unable to read combined.json",
            extra={"document_id": document_id, "error": type(exc).__name__},
        )
        return FormDriver()


def form_driver_note(details: FormDriver | None) -> str:
    """ "Driver details as read from the DMER form: licence ‹licence›, first
    name ‹first name›, surname ‹surname›." -- only the parts the form gave;
    "" when it gave none. A valid BC licence is shown in its canonical
    8-digit form (as the pipeline looked it up), anything else as read."""
    if details is None:
        return ""
    licence = normalize_licence(details.licence) or details.licence
    parts = [
        f"licence {licence}" if licence else "",
        f"first name {details.first_name}" if details.first_name else "",
        f"surname {details.last_name}" if details.last_name else "",
    ]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return f"{FORM_DRIVER_NOTE_PREFIX}: {', '.join(parts)}."
