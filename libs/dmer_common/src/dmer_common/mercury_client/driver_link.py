"""Mercury's ``add_driver``: link the driver the AI matched to the DMER (I-11).

``POST {MERCURY_WRITE_API_BASE_URL}/dmers/{documentRecordId}/add_driver``::

    {"driverLicenceNumber": "...", "firstName": "...", "surname": "...",
     "dateOfBirth": "<ISO 8601, optional>", "comments": "<optional>",
     "dmerId": "<documentRecordId>"}

Mercury links the driver if it exists, otherwise creates one, and answers
with the driver's open case, if any::

    {"resultMessage": "...", "driverOpenCase": {"caseId", "caseTitle",
     "caseType", "dmerStatus"}}

or, on error, ``{"errorStatus": 1, "replyMessage": "...", "caseId": ""}``
(handled by the client for every write).

The pipeline only sends it for a driver that **Mercury already holds**: the
one Driver Lookup matched from the licence read on the page. When no driver
matches, the DMER goes to Intake instead and no driver is created. The
licence and names are PII: they go to Mercury, never to logs.
"""

from __future__ import annotations

from typing import Any, Final

# Relative to MERCURY_WRITE_API_BASE_URL; {guid} is the DMER's
# documentRecordId (our document_guid), URL-quoted by the caller.
ADD_DRIVER_PATH: Final = "dmers/{guid}/add_driver"

# The note on the driver link: how the AI found the driver. No clinical
# content -- the outcome's reasoning goes with the outcome.
ADD_DRIVER_COMMENT: Final = (
    "Driver linked by the AI from the licence number read on the DMER."
)


def add_driver_request(payload: dict[str, Any]) -> dict[str, Any]:
    """The ``add_driver`` body from a ``MAP_DRIVER`` outbox row's payload
    (``document_guid``, ``licence_number``, ``first_name``, ``last_name``)."""
    return {
        "driverLicenceNumber": str(payload["licence_number"]),
        "firstName": str(payload["first_name"]),
        "surname": str(payload["last_name"]),
        "comments": ADD_DRIVER_COMMENT,
        "dmerId": str(payload["document_guid"]),
    }
