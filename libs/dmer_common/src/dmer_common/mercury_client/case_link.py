"""Mercury's ``add_case``: give a DMER a case (I-13).

``POST {MERCURY_WRITE_API_BASE_URL}/dmers/{documentRecordId}/add_case``::

    {"caseType": "DMER", "owner": "TBD",
     "driver": {"driverId": "<licence number>"},
     "documentType": "DMER", "comments": "..."}

(camelCase; ``caseType``, ``documentType`` and ``driver`` required --
Mercury DEV's Swagger, 2026-11-24) answers 201 ``{"caseId",
"documentRecordId", "status": "Created"}``. Mercury attaches a comment
(``comments``) to the driver and the case. 404 when the driver doesn't
exist; 409 when the DMER belongs to a different driver or already has a
case (errors as ``ProblemDetails``).

When it's called:

- **The AI linked the driver** (``add_driver`` was sent -- e.g. a DMER Mercury
  had under its placeholder driver, licence ``00000000``): decided from
  ``add_driver``'s answer (decided 2026-11-23). A real active case -> none;
  no active case, or the placeholder driver's case (title starting with
  ``00000000``) -> create one. ``add_driver`` for a real licence isn't
  expected to return the placeholder's case (to confirm with Mercury); if it
  does, the DMER still needs its own case.
- **Otherwise** (decided 2026-11-20): only for a DMER that has **no case** and
  whose driver has **no open case** -- checked live with Mercury's driver
  lookup just before. Otherwise nothing is sent and Intake handles the case.

``driverId`` is the driver's licence number (PII: never logged).

The driver's open case comes from the driver lookup's ``activeCase``
(``caseId``, ``caseTitle``, ``caseStatus``, ...).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from ..licence import PLACEHOLDER_LICENCE

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
    "Case created by the AI: the DMER needed a case and the driver had no active one."
)

# A driver's case with one of these statuses doesn't count as open.
_CLOSED_WORDS: Final = ("close", "cancel", "inactive", "complete")


def is_placeholder_case(case: Mapping[str, Any] | None) -> bool:
    """True when *case*'s title starts with the placeholder licence -- a case
    of Mercury's placeholder driver (``00000000``), not of the real driver."""
    if not case:
        return False
    # Titles start with the driver's licence ("<licence>- SURNAME- DMER- n",
    # e.g. "00000000- DFC- OTHR- 4" for the placeholder driver).
    title = str(case.get("caseTitle") or case.get("case_title") or "")
    return title.lstrip().startswith(PLACEHOLDER_LICENCE)


def add_case_request(licence_number: str) -> dict[str, Any]:
    """The ``add_case`` body for the driver with *licence_number*."""
    return {
        "caseType": CASE_TYPE,
        "owner": CASE_OWNER,
        "driver": {"driverId": licence_number},
        "documentType": DOCUMENT_TYPE,
        "comments": ADD_CASE_COMMENT,
    }


def _case_of(driver: Mapping[str, Any]) -> Mapping[str, Any] | None:
    # Mercury's lookup answers with ``activeCase``; the others are older
    # shapes (the pipeline's ``case``, GET dmers' ``dmerDocumentCaseRecord``).
    case = (
        driver.get("activeCase")
        or driver.get("case")
        or driver.get("dmerDocumentCaseRecord")
    )
    return case if isinstance(case, Mapping) else None


def open_case_id(lookup: Any) -> str | None:
    """The id of an open case in a ``drivers/lookup`` response (one driver, a
    list, or an ``items`` envelope), or None. A case without a status counts
    as open; one whose status reads closed/cancelled/inactive/complete
    doesn't, nor does a placeholder driver's case (:func:`is_placeholder_case`).
    An open case without an id is reported as ``"unknown"``."""
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
        if any(word in status for word in _CLOSED_WORDS) or is_placeholder_case(case):
            continue
        return str(case.get("case_id") or case.get("caseId") or "unknown")
    return None
