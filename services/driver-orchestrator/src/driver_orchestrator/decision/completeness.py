"""Decision Gateway step 1: is the driver's batch complete? (pure, no I/O)

See docs/development/stages/07-decision-gateway.md, step 1. The batch is ready
only when every DMER Mercury still holds as waiting for this driver has
finished its per-document processing here. Otherwise the evaluation is
recorded as WAITING -- an explicit state the Reconciliation Sweeper can find,
never a silent return.

"Verify twice": Driver Lookup set ``expected_document_count`` when the first
document arrived; this re-counts from a **fresh** Mercury ``GET by
driver_licence`` at decision time, because a new DMER may have arrived since.

Completeness is a count: Mercury's waiting DMERs against the batch's
finished documents. (Mercury's lookup now gives each document's id --
``document_guid`` after translation -- so it could match by id instead.)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

AWAITING = "AWAITING_DRIVER_COMPLETION"
MANUAL_REVIEW = "MANUAL_REVIEW"

# Defaults match Driver Lookup's configuration (MERCURY_COUNTED_DOCUMENT_TYPES,
# MERCURY_WAITING_DOCUMENT_STATUSES): DMERs whose status is Uploaded are
# waiting; any other status (Rejected, I-8, included) is not.
DEFAULT_COUNTED_TYPES = frozenset({"dmer"})
DEFAULT_WAITING_STATUSES = frozenset({"uploaded"})


class WaitReason(StrEnum):
    """Why a batch is not ready (logged and kept in the decision summary)."""

    MERCURY_UNVERIFIED = "mercury_unverified"  # no driver, or several
    DOCUMENTS_STILL_PROCESSING = "documents_still_processing"
    WAITING_FOR_DOCUMENTS = "waiting_for_documents"


class Member(Protocol):
    """What the check needs about one batch document (``BatchDocument`` from
    ``dmer_common.db.driver_decision`` satisfies it)."""

    @property
    def document_id(self) -> str: ...

    @property
    def pipeline_status(self) -> str: ...


@dataclass(frozen=True)
class BatchMember:
    """A plain :class:`Member` (tests, callers without a database row)."""

    document_id: str
    pipeline_status: str


@dataclass(frozen=True)
class Completeness:
    """The result of the check."""

    ready: bool
    expected: int | None  # Mercury's waiting DMERs (None: could not verify)
    done: int  # batch documents finished here (awaiting + manual review)
    to_decide: tuple[str, ...]  # documents to pass to decide(), when ready
    wait_reason: WaitReason | None = None


def mercury_waiting_count(
    drivers: Sequence[Mapping[str, Any]],
    *,
    counted_types: Iterable[str] = DEFAULT_COUNTED_TYPES,
    waiting_statuses: Iterable[str] = DEFAULT_WAITING_STATUSES,
    already_decided: Iterable[str] = (),
) -> int | None:
    """How many of the driver's DMERs are still waiting for a decision.

    Counts ``active_documents`` of a counted type whose ``document_status`` is
    a waiting status (default ``Uploaded``), except those whose
    ``document_guid`` is in *already_decided*. Mercury keeps a DMER
    ``Uploaded`` until a person reviews it -- through our AI Processed mark
    and our posted outcome -- so a DMER decided in an earlier batch is still
    ``Uploaded`` and only our own records can say it's done. ``dps_date`` is
    not consulted. Returns None unless Mercury returned exactly one driver --
    completeness can't be verified, so the batch waits.
    """
    if len(drivers) != 1:
        return None
    types = {t.strip().lower() for t in counted_types}
    waiting = {s.strip().lower() for s in waiting_statuses}
    decided = {g.strip().lower() for g in already_decided}
    return sum(
        1
        for d in waiting_documents(drivers[0], types, waiting)
        if str(d.get("document_guid") or "").strip().lower() not in decided
    )


def waiting_documents(
    driver: Mapping[str, Any], types: set[str], waiting: set[str]
) -> list[Mapping[str, Any]]:
    """The driver's documents of a counted type in a waiting status
    (*types* and *waiting* lower-case)."""
    return [
        d
        for d in driver.get("active_documents") or []
        if isinstance(d, Mapping)
        and str(d.get("document_type", "")).strip().lower() in types
        and str(d.get("document_status", "")).strip().lower() in waiting
    ]


def check_completeness(expected: int | None, batch: Sequence[Member]) -> Completeness:
    """Decide whether the batch is ready.

    - Mercury not verified -> wait.
    - Any batch document still in an earlier status (counted by the Rule
      Engine but not yet signalled) -> wait; its own signal re-triggers this.
    - Fewer finished documents than Mercury's waiting DMERs -> wait.
    - Otherwise ready. Documents that went to MANUAL_REVIEW after being
      counted are finished (they get a fallback outcome elsewhere) but are not
      decided here; only documents awaiting their driver are.

    More finished documents than Mercury expects (a DMER was triaged by a
    person meanwhile) is still ready.
    """
    awaiting = tuple(d.document_id for d in batch if d.pipeline_status == AWAITING)
    failed = sum(d.pipeline_status == MANUAL_REVIEW for d in batch)
    done = len(awaiting) + failed

    def wait(reason: WaitReason) -> Completeness:
        return Completeness(False, expected, done, (), reason)

    if expected is None:
        return wait(WaitReason.MERCURY_UNVERIFIED)
    if done < len(batch):
        return wait(WaitReason.DOCUMENTS_STILL_PROCESSING)
    if done < expected:
        return wait(WaitReason.WAITING_FOR_DOCUMENTS)
    return Completeness(True, expected, done, awaiting)
