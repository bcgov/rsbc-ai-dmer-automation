"""Decision Gateway outcome logic (pure functions; no I/O)."""

from .gateway import decide
from .models import BatchDecision, DocumentDecision, DocumentFacts, Path, RuleOutcome

__all__ = [
    "BatchDecision",
    "DocumentDecision",
    "DocumentFacts",
    "Path",
    "RuleOutcome",
    "decide",
]
