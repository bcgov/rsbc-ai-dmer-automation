"""Triage a normalized DMER with the LLM: one call, its answer validated
(and retried when invalid), then the rules engine's own outcome selection.

:func:`triage_document` returns the same :class:`~dmer_common.rules.RuleEvaluation`
the rules engine does, so the Rule Engine activity persists either engine's
result the same way; :class:`TriageResult` adds what the decision record
needs to say which model, prompt and procedures produced it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date

from ..normalization.errors import call_model
from ..openai_client import OpenAIClient
from ..rules import RuleEvaluation, select_outcome
from ..telemetry import get_logger
from .answer import checks, outcomes, validate
from .policy_store import PolicyText
from .prompt import PROMPT_VERSION, system_prompt
from .request import redact, user_content

_log = get_logger(__name__)

# Unset: the deployment's default, as in the POC's validated runs.
_TEMPERATURE = os.environ.get("TRIAGE_TEMPERATURE")

# Every key the answer schema has (see .prompt) -- for restoring a key the
# deployment masked with asterisks.
ANSWER_KEYS = frozenset(
    {
        "policy_source",
        "document",
        "version",
        "evaluation_status",
        "driver_class",
        "value",
        "evidence",
        "dmer_exam_date",
        "evaluation_date",
        "conditions_evaluated",
        "condition",
        "evidence_quality",
        "policy_reference",
        "chapter",
        "page",
        "condition_or_rule",
        "criteria_evaluated",
        "criterion",
        "observed_value",
        "required_value_or_condition",
        "result",
        "evidence_reference",
        "rule_status",
        "recommended_action",
        "fit_letter",
        "special_routing",
        "reasoning",
        "uncertainties",
        "overall_recommended_action",
        "overall_reasoning",
        "administrative_instructions",
        "missing_information",
        "ocr_verification_required",
        "human_review_reasons",
        "policy_conflicts",
        "audit_summary",
        "all_identified_conditions_evaluated",
        "correct_driver_class_rule_used",
        "date_rules_checked",
        "special_instructions_checked",
        "all_actions_supported_by_policy",
    }
)


@dataclass(frozen=True)
class TriageResult:
    """One triage: the selected outcome, and what produced it."""

    evaluation: RuleEvaluation
    answer: dict  # the model's full answer -- clinical content: never log
    checks: list[str] = field(default_factory=list)  # soft problems (names only)
    prompt_version: str = PROMPT_VERSION
    policy_sha256: str = ""
    policy_updated: date | None = None

    def record(self) -> dict:
        """The decision record for ``rule_evaluation.all_outcomes``: which
        engine, prompt and procedures; the candidates; and, per condition the
        model evaluated, its status, action and policy reference -- not its
        evidence or reasoning."""
        return {
            "engine": "triage_llm",
            "prompt_version": self.prompt_version,
            "policy_sha256": self.policy_sha256,
            "policy_updated": (
                self.policy_updated.isoformat() if self.policy_updated else None
            ),
            "evaluation_status": self.answer.get("evaluation_status"),
            "rule_engine_outcome_code": self.evaluation.rule_engine_outcome_code,
            "candidates": self.evaluation.all_outcomes(),
            "conditions": [
                {
                    "condition": c.get("condition"),
                    "rule_status": c.get("rule_status"),
                    "recommended_action": c.get("recommended_action"),
                    "fit_letter": c.get("fit_letter") is True,
                    "policy_reference": c.get("policy_reference"),
                }
                for c in self.answer.get("conditions_evaluated") or []
                if isinstance(c, dict)
            ],
            "checks": self.checks,
        }


def triage_document(
    openai: OpenAIClient,
    normalized: dict,
    policy: PolicyText,
    *,
    evaluation_date: date,
) -> TriageResult:
    """Triage one normalized DMER against *policy*.

    Raises :class:`~dmer_common.normalization.NormalizationValidationError`
    when the answer is still invalid after its retries -- poison for this
    document, routed to manual review like an unevaluable ruleset input.
    """
    sent = redact(normalized)
    messages = [
        {"role": "system", "content": system_prompt(policy.text, policy.updated)},
        {"role": "user", "content": user_content(sent, evaluation_date)},
    ]
    answer = call_model(
        openai,
        messages,
        temperature=float(_TEMPERATURE) if _TEMPERATURE else None,
        accept=validate,
        step="triage",
        source=sent,
        known_keys=ANSWER_KEYS,
    )
    evaluation = select_outcome(outcomes(answer, sent))
    problems = checks(answer, sent)
    _log.info(
        "triage evaluated",
        extra={
            "selected_outcome_code": evaluation.selected_outcome_code,
            "rule_engine_outcome_code": evaluation.rule_engine_outcome_code,
            "fit_letter": evaluation.fit_letter,
            "candidate_count": len(evaluation.outcomes),
            "evaluation_status": answer.get("evaluation_status"),
            "check_count": len(problems),
            "prompt_version": PROMPT_VERSION,
            "policy_sha256": policy.pdf_sha256,
        },
    )
    return TriageResult(
        evaluation=evaluation,
        answer=answer,
        checks=problems,
        policy_sha256=policy.pdf_sha256,
        policy_updated=policy.updated,
    )
