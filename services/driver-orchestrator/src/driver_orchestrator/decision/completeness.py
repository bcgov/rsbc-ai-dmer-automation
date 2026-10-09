"""Decision Gateway step 1: is the driver's batch complete? (pure, no I/O)

See docs/development/stages/07-decision-gateway.md, step 1. The batch is ready
only when every DMER Mercury still holds as waiting for this driver has
finished its per-document processing here. Otherwise the evaluation is
recorded as WAITING -- an explicit state the Reconciliation Sweeper can find,
never a silent return.

"Verify twice": Driver Lookup set ``expected_document_count`` when the first
document arrived; this re-counts from a **fresh** Mercury ``GET by
driver_licence`` at decision time, because a new DMER may have arrived since.

Mercury's ``active_documents`` carry no document id we can match to ours (the
mock API's shape; real Mercury's is open question M-5), so completeness is a
count: Mercury's waiting DMERs against the batch's finished documents.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

AWAITING = "AWAITING_DRIVER_COMPLETION"
MANUAL_REVIEW = "MANUAL_REVIEW"

# Defaults match Driver Lookup's configuration (MERCURY_COUNTED_DOCUMENT_TYPES,
# MERCURY_UNCOUNTED_DOCUMENT_STATUSES): DMERs count, Rejected ones don't (I-8).
DEFAULT_COUNTED_TYPES = frozenset({"dmer"})
DEFAULT_UNCOUNTED_STATUSES = frozenset({"rejected"})


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
    uncounted_statuses: Iterable[str] = DEFAULT_UNCOUNTED_STATUSES,
) -> int | None:
    """How many of the driver's DMERs Mercury still holds as not yet triaged.

    Counts ``active_documents`` of a counted type, not in an uncounted status,
    with an empty ``dps_date`` (I-9: empty means not yet triaged). Returns None
    unless Mercury returned exactly one driver -- completeness can't be
    verified, so the batch waits.
    """
    if len(drivers) != 1:
        return None
    types = {t.strip().lower() for t in counted_types}
    skipped = {s.strip().lower() for s in uncounted_statuses}
    documents = drivers[0].get("active_documents") or []
    return sum(
        1
        for d in documents
        if isinstance(d, Mapping)
        and str(d.get("document_type", "")).strip().lower() in types
        and str(d.get("document_status", "")).strip().lower() not in skipped
        and not str(d.get("dps_date") or "").strip()
    )


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
