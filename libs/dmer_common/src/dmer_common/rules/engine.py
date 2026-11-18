"""Evaluate a normalized DMER against a rules.json (GoRules ZEN) and select the outcome.

See docs/development/stages/05-activity-rule-engine.md. Every chapter table in
rules.json is a ``collect`` decision table that returns a list of candidate
outcomes (``action``, ``fit_letter``, ``reason``, and optionally
``driving_record_check``). The selected outcome is the highest-priority action;
every candidate is kept so it can be persisted, not just the winner.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime

import zen

from ..telemetry import get_logger

_log = get_logger(__name__)

# Lowest to highest priority.
OUTCOME_PRIORITY: tuple[str, ...] = ("CP", "IN", "PR", "PU", "TCM", "CR")

_DRIVING_RECORD_NOTE = (
    "the 5-year driving record must be checked for alcohol or drug related offences"
)
_NO_RULE_MATCHED = "No triage rule matched the DMER"


class RulesetError(ValueError):
    """The rules.json itself is unusable -- poison for every document, not just one."""


class RuleEvaluationError(ValueError):
    """This document can't be evaluated (e.g. the ruleset's input schema rejects it) -- poison."""


@dataclass(frozen=True)
class Outcome:
    """One candidate outcome returned by a decision table row."""

    section: str
    action: str
    fit_letter: bool
    reason: str
    driving_record_check: bool = False


@dataclass(frozen=True)
class RuleEvaluation:
    """The selected outcome plus every candidate that produced it."""

    outcomes: tuple[Outcome, ...]
    rule_engine_outcome_code: str
    selected_outcome_code: str
    selected_reason: str
    fit_letter: bool
    priority_rank: int

    def all_outcomes(self) -> list[dict]:
        """Every candidate as plain dicts, for ``rule_evaluation.all_outcomes``."""
        return [asdict(o) for o in self.outcomes]


class Ruleset:
    """A loaded rules.json. Build once per activity invocation and reuse for every document."""

    def __init__(self, content: bytes | str) -> None:
        raw = content.encode("utf-8") if isinstance(content, str) else content
        self.checksum = hashlib.sha256(raw).hexdigest()
        try:
            json.loads(raw)
            self._decision = zen.ZenEngine().create_decision(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RuntimeError) as exc:
            raise RulesetError(f"Invalid rules.json: {type(exc).__name__}") from None

    def evaluate(self, dmer: dict, *, received_date: datetime | str) -> RuleEvaluation:
        """Evaluate one normalized DMER (the unwrapped dict Normalize produces).

        ``received_date`` is the Mercury received date (``dmer_document.received_date``);
        it isn't part of the normalized document, but the monocular rules measure
        DMER age from it.
        """
        if isinstance(received_date, datetime):
            received_date = received_date.isoformat()
        try:
            result = self._decision.evaluate(
                {"dmer": {**dmer, "received_date": received_date}}
            )["result"]
        except RuntimeError as exc:
            raise RuleEvaluationError(
                f"Rule evaluation failed: {type(exc).__name__}"
            ) from None
        return select_outcome(_outcomes(result))


def _outcomes(result: dict) -> tuple[Outcome, ...]:
    outcomes = []
    for section, rows in result.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict) and row.get("action") in OUTCOME_PRIORITY:
                outcomes.append(
                    Outcome(
                        section=section,
                        action=row["action"],
                        fit_letter=row.get("fit_letter") is True,
                        reason=row.get("reason", ""),
                        driving_record_check=row.get("driving_record_check") is True,
                    )
                )
    return tuple(outcomes)


def select_outcome(outcomes: tuple[Outcome, ...]) -> RuleEvaluation:
    """Highest-priority action wins; reasons and FIT letters at that level are combined.

    The rule engine can't see the driving record, so a clean pass that any
    driving-record row fired for is sent to intake for that check instead
    (Drugs, Alcohol and Driving). The engine's own outcome is still reported
    as ``rule_engine_outcome_code``.
    """
    if not outcomes:
        return RuleEvaluation(
            (), "IN", "IN", _NO_RULE_MATCHED, False, OUTCOME_PRIORITY.index("IN")
        )

    top = max(OUTCOME_PRIORITY.index(o.action) for o in outcomes)
    winners = [o for o in outcomes if OUTCOME_PRIORITY.index(o.action) == top]
    engine_code = OUTCOME_PRIORITY[top]
    reason = ", and ".join(o.reason for o in winners)
    selected = engine_code
    if engine_code == "CP" and any(o.driving_record_check for o in outcomes):
        selected = "IN"
        reason = f"{reason}, and {_DRIVING_RECORD_NOTE}"
    return RuleEvaluation(
        outcomes=outcomes,
        rule_engine_outcome_code=engine_code,
        selected_outcome_code=selected,
        selected_reason=reason,
        fit_letter=any(o.fit_letter for o in winners),
        priority_rank=OUTCOME_PRIORITY.index(selected),
    )
