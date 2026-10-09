"""Inputs and outputs of the Decision Gateway's outcome logic.

Plain frozen dataclasses: the logic in :mod:`.gateway` is pure, so the
orchestration layer builds :class:`DocumentFacts` from the database
(``dmer_document``, ``dmer_extraction``, the latest ``rule_evaluation`` row) and
persists :class:`BatchDecision` through Post-Processing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class Path(StrEnum):
    """Which branch of the gateway decided a document (``decision_reason.path``)."""

    ATTACHED_REPORT = "attached_report"  # more than two pages
    ALL_CUT_OFF = "all_cut_off"
    CUT_OFF_SUPERSEDED = "cut_off_superseded"  # a clear copy exists
    DUPLICATE_RETAINED = "duplicate_retained"
    DUPLICATE_REJECTED = "duplicate_rejected"  # clean pass: older copy rejected
    DUPLICATE_FORWARDED = "duplicate_forwarded"  # not a clean pass: all forwarded
    DIFFERING_CONTENT = "differing_content"
    SINGLE_DOCUMENT = "single_document"
    NO_RULE_OUTCOME = "no_rule_outcome"


@dataclass(frozen=True)
class RuleOutcome:
    """The latest ``rule_evaluation`` row for a document."""

    outcome_code: str  # selected_outcome_code: CP, IN, PR, PU, PCM, CR
    # Higher wins: CP 0, IN 1, PR 2, PU 3 in rules sha256-bc8bd53103ff.
    priority_rank: int
    reason: str | None = None  # selected_reason -- clinical content: never log
    rules_version: str | None = None


@dataclass(frozen=True)
class DocumentFacts:
    """Everything the gateway needs about one waiting document of the driver."""

    document_id: str
    document_guid: str
    received_date: datetime | None
    page_count: int | None
    is_cutoff: bool | None
    comparison_hash: str | None
    comparison_fields: dict[str, Any] | None
    rule: RuleOutcome | None
    driver_key: str | None = None
    # How Driver Lookup found the driver: MERCURY_SUPPLIED (Mercury named
    # them) or LICENCE_LOOKUP (matched from the licence read off the page).
    driver_resolved_by: str | None = None


@dataclass(frozen=True)
class DocumentDecision:
    """The computed outcome for one document (a future ``dmer_decision`` row)."""

    document_id: str
    outcome_code: str
    path: Path
    is_duplicate: bool = False  # True only when the copy is to be rejected in Mercury
    duplicate_of_document_id: str | None = None
    superseded_by_cutoff_rule: bool = False
    # True when Mercury already links the DMER to the driver. When the driver
    # was matched from the page's licence, False with proposed_driver_key set,
    # so Post-Processing maps the driver in Mercury (I-11).
    driver_mapped: bool = False
    proposed_driver_key: str | None = None
    reason: dict[str, Any] = field(default_factory=dict)  # -> decision_reason (jsonb)


@dataclass(frozen=True)
class BatchDecision:
    """Outcomes for every document of one driver batch, plus the summary."""

    decisions: tuple[DocumentDecision, ...]
    summary: dict[str, Any]  # -> driver_evaluation.decision_summary (jsonb)
