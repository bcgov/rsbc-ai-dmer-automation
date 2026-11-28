"""Mercury's ``setDmerAIProccessedStatus``: the AI's result on one DMER.

``POST {MERCURY_WRITE_API_BASE_URL}/Dmers/{documentRecordId}/setDmerAIProccessedStatus``
(the DMER in the path -- Mercury DEV's Swagger, 2026-11-24) with::

    {"aiProcessedStatus": int, "aiDpsPriority": int, "aiQueue": int,
     "aiDmerStatus": int, "aiIsDuplicate": bool, "notes": "<comment>"}

``aiProcessedStatus`` is the only required field.

The integers are Mercury (Dynamics) option-set values. What each case sends
(confirmed with Mercury, 2026-11-20):

- an AI outcome: ``Proccessed``, and the priority, queue and status
  :data:`OUTCOME_ROUTING` gives the outcome; ``notes`` is the comment;
- a duplicate: ``Proccessed``, ``aiIsDuplicate`` true and a "Duplicate"
  note -- nothing else;
- a processing error (any fallback: the pipeline could not decide): ``Error``,
  routed to Intake as ``IN`` (Regular, Team - Intake, Adjudicate); ``notes``
  carries "Unable to AI process due to error" and the details;
- an outcome whose driver link (``add_driver``) failed: the same processing
  error, for Intake to review by hand; ``notes`` says the driver could not be
  linked, with Mercury's error, then the AI's own comment;
- an outcome whose case (``add_case``) could not be created: sent as usual,
  with ``notes`` adding that the case could not be created and why.

``notes`` carries clinical content: it goes to Mercury, never to logs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Final

# Relative to MERCURY_WRITE_API_BASE_URL; {guid} is the DMER's
# documentRecordId (our document_guid), URL-quoted by the caller. Mercury's
# spelling and casing.
AI_STATUS_PATH: Final = "Dmers/{guid}/setDmerAIProccessedStatus"


class AiProcessedStatus(IntEnum):
    NA = 100000000
    IN_PROGRESS = 100000001
    PROCESSED = 100000002  # Mercury's label: "Proccessed"
    ERROR = 100000003


class AiDmerStatus(IntEnum):
    ADJUDICATE = 100000000
    REJECT = 100000001
    CLEAN_PASS = 100000002
    MANUAL_PASS = 100000003


class AiQueue(IntEnum):
    TEAM_INTAKE = 100000000
    TEAM_ADJUDICATORS = 100000001
    TEAM_CASE_MANAGERS = 100000002
    TEAM_DIP_TEAM_LEAD = 100000003
    TEAM_REMEDIAL_INTAKE = 100000004
    TEAM_DIP_ADJUDICATORS = 100000005


class AiDpsPriority(IntEnum):
    REGULAR = 100000000
    URGENT_IMMEDIATE = 100000001
    EXPEDITED = 100000002
    CRITICAL_REVIEW = 100000003


@dataclass(frozen=True)
class Routing:
    """Where an outcome sends the DMER in Mercury."""

    priority: AiDpsPriority
    queue: AiQueue
    status: AiDmerStatus


OUTCOME_ROUTING: Final[dict[str, Routing]] = {
    "CP": Routing(AiDpsPriority.REGULAR, AiQueue.TEAM_INTAKE, AiDmerStatus.MANUAL_PASS),
    "IN": Routing(AiDpsPriority.REGULAR, AiQueue.TEAM_INTAKE, AiDmerStatus.ADJUDICATE),
    "PR": Routing(
        AiDpsPriority.REGULAR, AiQueue.TEAM_ADJUDICATORS, AiDmerStatus.ADJUDICATE
    ),
    "PU": Routing(
        AiDpsPriority.URGENT_IMMEDIATE,
        AiQueue.TEAM_ADJUDICATORS,
        AiDmerStatus.ADJUDICATE,
    ),
    "TCM": Routing(
        AiDpsPriority.REGULAR, AiQueue.TEAM_CASE_MANAGERS, AiDmerStatus.ADJUDICATE
    ),
    "CR": Routing(
        AiDpsPriority.CRITICAL_REVIEW,
        AiQueue.TEAM_ADJUDICATORS,
        AiDmerStatus.ADJUDICATE,
    ),
}

# A processing error goes to Intake as IN.
ERROR_OUTCOME: Final = "IN"
DUPLICATE_NOTE: Final = "Duplicate"
# Same wording as the pipeline's fallback comment (I-1).
PROCESSING_ERROR_NOTE: Final = "Unable to AI process due to error"


class UnknownOutcomeError(ValueError):
    """An outcome code with no routing -- the row can never be sent as is."""


def ai_status_request(
    *,
    outcome_code: str | None,
    notes: str | None,
    processing_error: bool = False,
    is_duplicate: bool = False,
    duplicate_of_document_guid: str | None = None,
    driver_link_error: str | None = None,
    case_error: str | None = None,
    form_driver_note: str | None = None,
) -> dict[str, Any]:
    """The ``setDmerAIProccessedStatus`` body for one DMER (the DMER itself
    is in the path, :data:`AI_STATUS_PATH`).

    *processing_error*: the pipeline could not decide (a fallback); the DMER
    goes to Intake as IN with status Error. *is_duplicate*: only the
    duplicate flag and note are sent. *driver_link_error*: linking the
    driver failed -- sent as a processing error whatever the outcome (or
    duplicate), so Intake reviews it; *form_driver_note* (the driver's
    details as read from the form, :func:`dmer_common.form_driver.form_driver_note`)
    follows the error in the notes. *case_error*: creating the DMER's case
    failed -- the outcome (or duplicate) goes as usual, the error added to the
    notes. Raises :class:`UnknownOutcomeError` for an outcome code
    :data:`OUTCOME_ROUTING` doesn't know.
    """
    case_note = (
        f"The case could not be created for this DMER "
        f"({case_error.strip().rstrip('.')})."
        if case_error
        else ""
    )
    if driver_link_error:
        processing_error = True
        detail = (
            f"{PROCESSING_ERROR_NOTE}: the driver could not be linked to the DMER "
            f"({driver_link_error.strip().rstrip('.')})."
        )
        if form_driver_note and form_driver_note.strip():
            detail = f"{detail} {form_driver_note.strip()}"
        notes = f"{detail} {notes}".strip() if notes else detail
    elif is_duplicate:
        note = DUPLICATE_NOTE
        if duplicate_of_document_guid:
            note = f"{DUPLICATE_NOTE} of DMER {duplicate_of_document_guid}."
        if case_note:
            note = f"{note} {case_note}"
        return {
            "aiProcessedStatus": int(AiProcessedStatus.PROCESSED),
            "aiIsDuplicate": True,
            "notes": note,
        }
    code = ERROR_OUTCOME if processing_error else str(outcome_code or "").upper()
    routing = OUTCOME_ROUTING.get(code)
    if routing is None:
        raise UnknownOutcomeError(f"no Mercury routing for outcome {code!r}")
    status = (
        AiProcessedStatus.ERROR if processing_error else AiProcessedStatus.PROCESSED
    )
    if case_note and not driver_link_error:
        notes = f"{notes} {case_note}".strip() if notes else case_note
    return {
        "aiProcessedStatus": int(status),
        "aiDpsPriority": int(routing.priority),
        "aiQueue": int(routing.queue),
        "aiDmerStatus": int(routing.status),
        "notes": notes or "",
    }
