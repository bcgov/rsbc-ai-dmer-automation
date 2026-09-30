"""Pipeline status modelling for ``dmer_document`` (revised architecture).

Status is modelled as **two orthogonal fields**, not one enum (see
``docs/development/data-model.md`` §Status modelling):

- :class:`PipelineStatus` — *how* the document is doing (its lifecycle/health).
- :class:`PipelineStage` — *where* the document is (its current stage).

Keeping the transition table here as pure data makes the state machine
unit-testable independently of any database.

di-processor drives the extraction slice of the lifecycle:

    RECEIVED -> DOWNLOADED -> EXTRACTING -> EXTRACTED

``MANUAL_REVIEW`` is reachable from any non-terminal status (retries exhausted or
a poison/DLQ path). The stages beyond ``EXTRACTED`` are owned by downstream
services and are included here so the shared enum is complete.
"""

from __future__ import annotations

import enum


class PipelineStage(str, enum.Enum):
    """Where a document is in the pipeline (``dmer_document.current_stage``)."""

    INGEST = "INGEST"
    EXTRACT = "EXTRACT"
    NORMALIZE = "NORMALIZE"
    RULES = "RULES"
    DECISION = "DECISION"
    POST = "POST"


class PipelineStatus(str, enum.Enum):
    """How a document is doing (``dmer_document.pipeline_status``)."""

    RECEIVED = "RECEIVED"
    DOWNLOADED = "DOWNLOADED"
    EXTRACTING = "EXTRACTING"
    EXTRACTED = "EXTRACTED"
    NORMALIZED = "NORMALIZED"
    RULES_APPLIED = "RULES_APPLIED"
    AWAITING_DRIVER_COMPLETION = "AWAITING_DRIVER_COMPLETION"
    DECIDED = "DECIDED"
    POSTING = "POSTING"
    COMPLETED = "COMPLETED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


# Forward, happy-path transitions across the whole pipeline. ``MANUAL_REVIEW`` is
# added to every non-terminal status below (retries exhausted / poison).
_FORWARD: dict[PipelineStatus, set[PipelineStatus]] = {
    PipelineStatus.RECEIVED: {PipelineStatus.DOWNLOADED},
    PipelineStatus.DOWNLOADED: {PipelineStatus.EXTRACTING},
    PipelineStatus.EXTRACTING: {PipelineStatus.EXTRACTED},
    PipelineStatus.EXTRACTED: {PipelineStatus.NORMALIZED},
    PipelineStatus.NORMALIZED: {PipelineStatus.RULES_APPLIED},
    PipelineStatus.RULES_APPLIED: {PipelineStatus.AWAITING_DRIVER_COMPLETION},
    PipelineStatus.AWAITING_DRIVER_COMPLETION: {PipelineStatus.DECIDED},
    PipelineStatus.DECIDED: {PipelineStatus.POSTING},
    PipelineStatus.POSTING: {PipelineStatus.COMPLETED},
    PipelineStatus.COMPLETED: set(),
    PipelineStatus.MANUAL_REVIEW: set(),
}

_TERMINAL: frozenset[PipelineStatus] = frozenset(
    {PipelineStatus.COMPLETED, PipelineStatus.MANUAL_REVIEW}
)


def next_statuses(current: PipelineStatus) -> set[PipelineStatus]:
    """Return the set of statuses reachable from ``current``."""
    allowed = set(_FORWARD[current])
    if current not in _TERMINAL:
        allowed.add(PipelineStatus.MANUAL_REVIEW)
    return allowed


def is_valid_transition(current: PipelineStatus, target: PipelineStatus) -> bool:
    """Return True if moving ``current`` -> ``target`` is allowed."""
    return target in next_statuses(current)
