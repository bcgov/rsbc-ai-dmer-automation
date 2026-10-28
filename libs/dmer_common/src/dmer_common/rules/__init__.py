"""Rule Engine library: evaluate a normalized DMER against rules.json (GoRules ZEN)."""

from .engine import (
    OUTCOME_PRIORITY,
    Outcome,
    RuleEvaluation,
    RuleEvaluationError,
    Ruleset,
    RulesetError,
    select_outcome,
)

__all__ = [
    "OUTCOME_PRIORITY",
    "Outcome",
    "RuleEvaluation",
    "RuleEvaluationError",
    "Ruleset",
    "RulesetError",
    "select_outcome",
]
