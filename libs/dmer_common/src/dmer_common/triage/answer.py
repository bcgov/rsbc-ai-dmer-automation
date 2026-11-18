"""The triage LLM's answer: validated, checked, and turned into the candidate
outcomes the rules engine's :func:`~dmer_common.rules.select_outcome` picks
from -- so the winner, FIT letter, reason and driving-record handling are the
rules engine's own, whichever engine produced the candidates.

- :func:`validate` -- what :func:`select_outcome` relies on (statuses, action
  codes, an action exactly when a rule was applied). A failure is retried.
- :func:`checks` -- everything else the answer should satisfy (evidence quotes
  the input, cited fields and pages exist). Recorded with the decision, never
  retried: they don't change the outcome.
- :func:`outcomes` -- APPLIED conditions, plus an IN candidate for each policy
  gap and for a request for human review, plus the driving-record check.

The driving-record check uses exactly the fields the rules engine's rows do
(:func:`_driving_record_rule_fires`), not the model's view of which rule
applied: deterministic, and the same outcomes as the POC and the rules engine
(POC DMER5/DMER16, where the model applied a drug rule in some runs only,
stay CP).
"""

from __future__ import annotations

import json
import re

from ..normalization.errors import InvalidModelOutput
from ..rules import OUTCOME_PRIORITY, Outcome

STATUSES = frozenset(
    {
        "RULE_MATCHED",
        "RULE_MATCHED_WITH_UNCERTAINTY",
        "HUMAN_REVIEW_REQUIRED",
        "INSUFFICIENT_INFORMATION",
    }
)
RULE_STATUSES = frozenset({"APPLIED", "NOT_APPLICABLE", "POLICY_GAP"})
POLICY_PAGES = 42  # pages in the procedures PDF the prompt cites

_POLICY_GAP_REASON = "The procedures have no rule that fits the documented condition"
_REVIEW_REASON = "The triage evaluation asked for human review"
_DRIVING_RECORD_REASON = (
    "Prescribed or psychotropic medication, or substance or alcohol use, is "
    "documented (Drugs, Alcohol and Driving)"
)


def _code(action: object) -> str | None:
    """An action code without the FIT-letter asterisk, or None for no action."""
    if action in (None, ""):
        return None
    return str(action).strip().rstrip("*")


def _conditions(answer: dict) -> list[dict]:
    return [c for c in answer.get("conditions_evaluated") or [] if isinstance(c, dict)]


def validate(answer: dict) -> dict:
    """The answer, when :func:`outcomes` can read it; raises
    :class:`InvalidModelOutput` (the call is retried) otherwise. Messages name
    statuses and codes only, never DMER content."""
    if answer.get("evaluation_status") not in STATUSES:
        raise InvalidModelOutput("evaluation_status is not one of the four statuses")
    conditions = answer.get("conditions_evaluated")
    if not isinstance(conditions, list) or any(
        not isinstance(c, dict) for c in conditions
    ):
        raise InvalidModelOutput("conditions_evaluated must be a list of objects")
    for i, condition in enumerate(conditions):
        status = condition.get("rule_status")
        if status not in RULE_STATUSES:
            raise InvalidModelOutput(f"condition {i}: unknown rule_status")
        action = _code(condition.get("recommended_action"))
        if action is not None and action not in OUTCOME_PRIORITY:
            raise InvalidModelOutput(f"condition {i}: action is not a policy code")
        if (status == "APPLIED") != (action is not None):
            raise InvalidModelOutput(
                f"condition {i}: rule_status {status} with "
                + ("an action" if action else "no action")
            )
    return answer


# "field.name: value" -- several per quote ('a: "x"; b: "y"'), or one unquoted.
# Field names can hold "/" (visual_acuity.vision_20/50_or_better).
_QUOTED_PAIR = re.compile(r'([\w./]+)\s*:\s*"([^"]*)"')
_SINGLE_PAIR = re.compile(r"\s*([\w./]+)\s*:\s*(.*?)\s*$", re.DOTALL)


def _pairs(quote: object) -> list[tuple[str, str]]:
    found = _QUOTED_PAIR.findall(str(quote))
    if not found:
        match = _SINGLE_PAIR.match(str(quote))
        found = [(match[1], match[2])] if match else []
    return found


def _quotes(condition: dict) -> list:
    return list(condition.get("evidence") or []) + [
        c.get("evidence_reference")
        for c in condition.get("criteria_evaluated") or []
        if isinstance(c, dict) and c.get("evidence_reference")
    ]


def checks(answer: dict, sent: dict) -> list[str]:
    """Soft problems with a valid answer: evidence that doesn't quote the
    input, unknown fields, impossible pages, and an overall action that isn't
    the highest condition action. Recorded with the decision; they name
    fields and pages, never values."""
    problems = []
    for i, condition in enumerate(_conditions(answer)):
        page = (condition.get("policy_reference") or {}).get("page")
        if page is not None:
            try:
                pages = [int(p) for p in re.split(r"\s*[-–,&]\s*", str(page).strip())]
                if not all(1 <= p <= POLICY_PAGES for p in pages):
                    problems.append(f"condition {i}: cites a page that does not exist")
            except ValueError:
                problems.append(f"condition {i}: page is not a page number")
        for field, value in (pair for q in _quotes(condition) for pair in _pairs(q)):
            field = field.removeprefix("fields.")
            if field.startswith("field_sources."):
                if field.removeprefix("field_sources.") not in (
                    sent.get("field_sources") or {}
                ):
                    problems.append(
                        f"condition {i}: cites an unknown field_sources entry"
                    )
                continue
            if field not in sent:
                if field != "evaluation_date":
                    problems.append(f"condition {i}: cites unknown field {field}")
                continue
            held = (
                sent[field] if isinstance(sent[field], str) else json.dumps(sent[field])
            )
            have = " ".join(held.lower().split())
            if not isinstance(sent[field], str):
                value = value.split(";")[0]
            # The model shortens quotes with "...": every piece must be in the value.
            pieces = (
                " ".join(p.lower().split()).strip(" ,;")
                for p in re.split(r"\.\.\.|…", value)
            )
            if any(p and p not in have for p in pieces):
                problems.append(
                    f"condition {i}: evidence for {field} does not match the input"
                )
    levels = [
        _code(c.get("recommended_action"))
        for c in _conditions(answer)
        if c.get("rule_status") == "APPLIED"
    ]
    highest = max(levels, key=OUTCOME_PRIORITY.index) if levels else None
    if _code(answer.get("overall_recommended_action")) != highest:
        problems.append("overall action is not the highest condition action")
    return problems


def _driving_record_rule_fires(sent: dict) -> bool:
    """Whether a rules.json row with ``driving_record_check`` would fire --
    the Psychotropic Drugs table's prescribed-medication, psychiatric-
    medication and substance/alcohol rows (Drugs, Alcohol and Driving)."""
    return (
        sent.get("psychotropic_drugs.perscribed_drugs") is True
        or sent.get("psychotropic_drugs.substance_abuse_or_dependence") is True
        or (
            sent.get("psychiatric.on_medications") is True
            and (
                sent.get("psychiatric.compliant_with_treatment") is True
                or sent.get("psychiatric.non_compliant_with_treatment") is True
            )
        )
    )


def outcomes(answer: dict, sent: dict) -> tuple[Outcome, ...]:
    """The candidates for :func:`select_outcome`:

    - each APPLIED condition, with its action and FIT letter;
    - IN for each POLICY_GAP -- a documented condition the procedures can't
      place is never a clean pass;
    - IN when the model itself asked for human review;
    - a CP carrying the driving-record flag when a rules-engine row with
      ``driving_record_check`` would fire -- so a clean pass is sent to intake
      for the driving-record check, as with the rules engine.

    None at all: :func:`select_outcome` gives IN, "No triage rule matched".
    """
    found: list[Outcome] = []
    for condition in _conditions(answer):
        name = str(condition.get("condition") or "")
        reference = condition.get("policy_reference") or {}
        if condition.get("rule_status") == "APPLIED":
            found.append(
                Outcome(
                    section=str(reference.get("chapter") or name),
                    action=_code(condition.get("recommended_action")),
                    fit_letter=condition.get("fit_letter") is True,
                    reason=str(reference.get("condition_or_rule") or name),
                )
            )
        elif condition.get("rule_status") == "POLICY_GAP":
            found.append(
                Outcome(
                    section=str(reference.get("chapter") or name),
                    action="IN",
                    fit_letter=False,
                    reason=(
                        f"{_POLICY_GAP_REASON} ({name})" if name else _POLICY_GAP_REASON
                    ),
                )
            )
    # A policy gap already asks for review (the prompt requires the status
    # with one), and its IN says why; this IN is for a review asked otherwise.
    gap = any(c.get("rule_status") == "POLICY_GAP" for c in _conditions(answer))
    if answer.get("evaluation_status") == "HUMAN_REVIEW_REQUIRED" and not gap:
        found.append(
            Outcome(
                section="Review", action="IN", fit_letter=False, reason=_REVIEW_REASON
            )
        )
    if _driving_record_rule_fires(sent):
        found.append(
            Outcome(
                section="Drugs, Alcohol and Driving",
                action="CP",
                fit_letter=False,
                reason=_DRIVING_RECORD_REASON,
                driving_record_check=True,
            )
        )
    return tuple(found)
