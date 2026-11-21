"""Mercury's ``add_case``: give a DMER a case (I-13).

``POST {MERCURY_WRITE_API_BASE_URL}/dmers/{documentRecordId}/add_case``::

    {"case_type": "DMER", "owner": "TBD",
     "driver": {"driver_id": "<licence number>"},
     "document_type": "DMER", "comments": "..."}

answers ``{"caseId", "documentRecordId", "status": "Created"}``. Mercury
attaches a comment (``comments``) to the driver and the case. 404 when the
driver doesn't exist; 409 when the DMER belongs to a different driver or
already has a case.

When it's called (decided 2026-11-20): only for a DMER that has **no case**
and whose driver has **no open case** -- checked live with Mercury's driver
lookup just before. Otherwise nothing is sent and Intake handles the case.
``driver_id`` is the driver's licence number (PII: never logged).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

# Relative to MERCURY_WRITE_API_BASE_URL; {guid} is the DMER's
# documentRecordId (our document_guid), URL-quoted by the caller.
ADD_CASE_PATH: Final = "dmers/{guid}/add_case"
# Mercury's driver lookup, relative to the same base (the Mercury host).
DRIVER_LOOKUP_PATH: Final = "drivers/lookup"

# The values Mercury's sample uses; to revisit with Mercury (owner "TBD").
CASE_TYPE: Final = "DMER"
CASE_OWNER: Final = "TBD"
DOCUMENT_TYPE: Final = "DMER"
ADD_CASE_COMMENT: Final = (
    "Case created by the AI: the DMER had no case and the driver had no open case."
)

# A driver's case with one of these statuses doesn't count as open.
_CLOSED_WORDS: Final = ("close", "cancel", "inactive", "complete")


def add_case_request(licence_number: str) -> dict[str, Any]:
    """The ``add_case`` body for the driver with *licence_number*."""
    return {
        "case_type": CASE_TYPE,
        "owner": CASE_OWNER,
        "driver": {"driver_id": licence_number},
        "document_type": DOCUMENT_TYPE,
        "comments": ADD_CASE_COMMENT,
    }


def _case_of(driver: Mapping[str, Any]) -> Mapping[str, Any] | None:
    case = driver.get("case") or driver.get("dmerDocumentCaseRecord")
    return case if isinstance(case, Mapping) else None


def open_case_id(lookup: Any) -> str | None:
    """The id of an open case in a ``drivers/lookup`` response (one driver, a
    list, or an ``items`` envelope), or None. A case without a status counts
    as open; one whose status reads closed/cancelled/inactive/complete
    doesn't. An open case without an id is reported as ``"unknown"``."""
    if isinstance(lookup, Mapping) and isinstance(lookup.get("items"), list):
        lookup = lookup["items"]
    drivers = lookup if isinstance(lookup, list) else [lookup]
    for driver in drivers:
        if not isinstance(driver, Mapping):
            continue
        case = _case_of(driver)
        if not case:
            continue
        status = str(case.get("case_status") or case.get("caseStatus") or "").lower()
        if any(word in status for word in _CLOSED_WORDS):
            continue
        return str(case.get("case_id") or case.get("caseId") or "unknown")
    return None
