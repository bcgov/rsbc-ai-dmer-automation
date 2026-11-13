"""Triage LLM: triage a normalized DMER against the Triage Sort Procedures.

The decision engine the Rule Engine activity uses when ``DECISION_ENGINE`` is
``triage_llm`` (see docs/development/stages/05-activity-rule-engine.md). The
procedures PDF is turned into the text the model reads by :mod:`.policy_text`,
once per PDF version (:mod:`.policy_store`).
"""

from __future__ import annotations

from .engine import TriageResult, triage_document
from .policy_store import PolicyText, PolicyUnavailableError, load_policy
from .policy_text import PolicyTextError, build, convert, updated_date, verify
from .prompt import PROMPT_VERSION

__all__ = [
    "PROMPT_VERSION",
    "PolicyText",
    "PolicyTextError",
    "PolicyUnavailableError",
    "TriageResult",
    "build",
    "convert",
    "load_policy",
    "triage_document",
    "updated_date",
    "verify",
]
