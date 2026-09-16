"""Document processing status enum and allowed transitions.

Status lifecycle (di-processor):

    received -> extracting -> sectioning -> combining -> combined -> published

``failed`` is reachable from any non-terminal status. ``published`` and
``failed`` are terminal. Keeping the transition table here (pure data) makes the
state machine unit-testable independently of any database.
"""

from __future__ import annotations

import enum


class DocumentStatus(str, enum.Enum):
    """Processing status for a document row."""

    RECEIVED = "received"
    EXTRACTING = "extracting"
    SECTIONING = "sectioning"
    COMBINING = "combining"
    COMBINED = "combined"
    PUBLISHED = "published"
    FAILED = "failed"


# Forward, happy-path transitions. ``failed`` is added to every non-terminal
# status below.
_FORWARD: dict[DocumentStatus, set[DocumentStatus]] = {
    DocumentStatus.RECEIVED: {DocumentStatus.EXTRACTING},
    DocumentStatus.EXTRACTING: {DocumentStatus.SECTIONING},
    DocumentStatus.SECTIONING: {DocumentStatus.COMBINING},
    DocumentStatus.COMBINING: {DocumentStatus.COMBINED},
    DocumentStatus.COMBINED: {DocumentStatus.PUBLISHED},
    DocumentStatus.PUBLISHED: set(),
    DocumentStatus.FAILED: set(),
}

_TERMINAL: frozenset[DocumentStatus] = frozenset(
    {DocumentStatus.PUBLISHED, DocumentStatus.FAILED}
)


def next_statuses(current: DocumentStatus) -> set[DocumentStatus]:
    """Return the set of statuses reachable from ``current``."""
    allowed = set(_FORWARD[current])
    if current not in _TERMINAL:
        allowed.add(DocumentStatus.FAILED)
    return allowed


def is_valid_transition(current: DocumentStatus, target: DocumentStatus) -> bool:
    """Return True if moving ``current`` -> ``target`` is allowed."""
    return target in next_statuses(current)
