"""Triage LLM: triage a normalized DMER against the Triage Sort Procedures.

The decision engine the Rule Engine activity uses when ``DECISION_ENGINE`` is
``triage_llm`` (see docs/development/stages/05-activity-rule-engine.md). The
procedures PDF is turned into the text the model reads by :mod:`.policy_text`.
"""

from __future__ import annotations

from .policy_text import PolicyTextError, build, convert, updated_date, verify

__all__ = [
    "PolicyTextError",
    "build",
    "convert",
    "updated_date",
    "verify",
]
