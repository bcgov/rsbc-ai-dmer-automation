"""Decision Gateway outcome logic: one driver's waiting DMERs -> one outcome each.

Pure functions, no I/O. The completeness check (Mercury ``GET by
driver_licence``) runs before this and must pass first: every document passed
in has reached ``RULES_APPLIED``.

Rules follow the Intake business sign-off (Sep 29, 2026; architecture v2.1
§4.4.3), applied in order:

1. **Attached reports.** A DMER with more than two pages is IN ("medical report
   attached") and takes no part in the comparison below.
2. **Cut-off.** If every remaining copy is cut off, each is IN, with a note so
   the reviewer can ask for a clearer copy. Otherwise a cut-off copy is treated
   as a duplicate of the retained clear copy; completeness always beats recency.
3. **Duplicates.** Clear copies with the same ``comparison_hash`` are
   duplicates. If every copy is a clean pass (CP), the newest is kept and the
   older ones are rejected. Otherwise nothing is rejected: all copies are
   forwarded together with the highest-priority outcome among them.
4. **Differing content.** If the clear copies are not all duplicates of each
   other, every copy is IN, with the fields that differ in the reason.
5. **Single document.** The rule engine outcome is kept.

Where the sign-off is silent or an answer is pending, the safe default is IN
(open questions I-19 to I-21 in the architecture document).

``decision_reason`` carries the rule engine's reason (clinical content): it
goes to the database and the Mercury comment, never to logs.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from .models import BatchDecision, DocumentDecision, DocumentFacts, Path

CLEAN_PASS = "CP"
MANUAL_REVIEW = "IN"
MAX_AUTOMATED_PAGES = 2

NOTE_ATTACHED_REPORT = (
    "Medical report attached (more than two pages): not processed automatically."
)
NOTE_ALL_CUT_OFF = (
    "Every copy of this DMER is incomplete (top or bottom cut off): consider "
    "requesting a clearer copy."
)
NOTE_NO_RULE_OUTCOME = "No rule engine outcome recorded: sent for manual review."
NOTE_DIFFERING = "Waiting DMERs for this driver differ: sent for manual review."
NOTE_COPIES_DISAGREE = (
    "Duplicate copies received different rule engine outcomes; the "
    "highest-priority outcome was applied and no copy was rejected."
)


def decide(documents: Sequence[DocumentFacts]) -> BatchDecision:
    """Compute the outcome for every waiting document of one driver."""
    if not documents:
        raise ValueError("decide() needs at least one document")

    decisions: list[DocumentDecision] = []
    remaining: list[DocumentFacts] = []
    for doc in documents:
        if doc.page_count is not None and doc.page_count > MAX_AUTOMATED_PAGES:
            decisions.append(
                _decision(
                    doc, MANUAL_REVIEW, Path.ATTACHED_REPORT, NOTE_ATTACHED_REPORT
                )
            )
        elif doc.rule is None:
            # Completeness should prevent this; never guess an outcome.
            decisions.append(
                _decision(
                    doc, MANUAL_REVIEW, Path.NO_RULE_OUTCOME, NOTE_NO_RULE_OUTCOME
                )
            )
        else:
            remaining.append(doc)

    if remaining:
        # Unknown cut-off state (None) counts as cut off: the safe direction.
        clear = [d for d in remaining if d.is_cutoff is False]
        cut_off = [d for d in remaining if d.is_cutoff is not False]
        if not clear:
            decisions.extend(
                _decision(d, MANUAL_REVIEW, Path.ALL_CUT_OFF, NOTE_ALL_CUT_OFF)
                for d in cut_off
            )
        else:
            decisions.extend(_decide_clear(clear, cut_off))

    by_id = {d.document_id: d for d in decisions}
    ordered = tuple(by_id[doc.document_id] for doc in documents)  # input order
    return BatchDecision(decisions=ordered, summary=_summary(ordered))


def _decide_clear(
    clear: list[DocumentFacts], cut_off: list[DocumentFacts]
) -> list[DocumentDecision]:
    groups = _duplicate_groups(clear)
    if len(groups) > 1:
        return _differing(groups, cut_off)

    group = groups[0]  # newest first
    retained, older = group[0], group[1:]
    if not older and not cut_off:
        return [_decision(retained, retained.rule.outcome_code, Path.SINGLE_DOCUMENT)]

    codes = [d.rule.outcome_code for d in group]  # cut-off copies' outcomes ignored
    if all(code == CLEAN_PASS for code in codes):
        # Clean pass: keep the newest, reject the older copies and the cut-offs.
        out = [_decision(retained, CLEAN_PASS, Path.DUPLICATE_RETAINED)]
        out += [
            _decision(
                d,
                CLEAN_PASS,
                Path.DUPLICATE_REJECTED,
                is_duplicate=True,
                duplicate_of=retained.document_id,
            )
            for d in older
        ]
        out += [
            _decision(
                d,
                CLEAN_PASS,
                Path.CUT_OFF_SUPERSEDED,
                is_duplicate=True,
                duplicate_of=retained.document_id,
                superseded=True,
            )
            for d in cut_off
        ]
        return out

    # Not a clean pass: reject nothing, forward every copy with the
    # highest-priority outcome in the group (the rule engine's own precedence).
    top = max(group, key=lambda d: d.rule.priority_rank).rule
    notes = [NOTE_COPIES_DISAGREE] if len(set(codes)) > 1 else []
    related = [d.document_id for d in (*group, *cut_off)]
    out = [
        _decision(
            d,
            top.outcome_code,
            Path.DUPLICATE_RETAINED if d is retained else Path.DUPLICATE_FORWARDED,
            *notes,
            applied_rule=top.reason,
            duplicate_group=related,
        )
        for d in group
    ]
    out += [
        _decision(
            d,
            top.outcome_code,
            Path.CUT_OFF_SUPERSEDED,
            *notes,
            superseded=True,
            applied_rule=top.reason,
            duplicate_group=related,
        )
        for d in cut_off
    ]
    return out


def _differing(
    groups: list[list[DocumentFacts]], cut_off: list[DocumentFacts]
) -> list[DocumentDecision]:
    """Clear copies that are not all duplicates: everything goes to IN.

    The I-6 answer ("process the newest, mark the older rejected") is pending
    Intake confirmation and is not in the sign-off, so nothing is rejected.
    """
    newest = [g[0] for g in groups]
    diff = _field_diff(newest)
    out = [
        _decision(
            d,
            MANUAL_REVIEW,
            Path.DIFFERING_CONTENT,
            NOTE_DIFFERING,
            differing_fields=diff,
        )
        for group in groups
        for d in group
    ]
    out += [
        _decision(
            d,
            MANUAL_REVIEW,
            Path.CUT_OFF_SUPERSEDED,
            NOTE_DIFFERING,
            superseded=True,
            differing_fields=diff,
        )
        for d in cut_off
    ]
    return out


def _duplicate_groups(docs: Iterable[DocumentFacts]) -> list[list[DocumentFacts]]:
    """Group by comparison_hash (no hash -> its own group); newest first in each."""
    groups: dict[str, list[DocumentFacts]] = {}
    for doc in docs:
        key = doc.comparison_hash or f"no-hash:{doc.document_id}"
        groups.setdefault(key, []).append(doc)
    ordered = [sorted(g, key=_recency, reverse=True) for g in groups.values()]
    return sorted(ordered, key=lambda g: _recency(g[0]), reverse=True)


def _recency(doc: DocumentFacts) -> tuple[float, str]:
    """Sort key for "newest": received_date, then document_guid (deterministic)."""
    received = doc.received_date.timestamp() if doc.received_date else float("-inf")
    return received, doc.document_guid


def _field_diff(docs: list[DocumentFacts]) -> dict[str, dict[str, Any]]:
    """{field: {document_id: value}} for every comparison field that differs."""
    fields = [d.comparison_fields or {} for d in docs]
    keys = sorted(set().union(*fields) - {"version"})
    return {
        key: {d.document_id: f.get(key) for d, f in zip(docs, fields, strict=True)}
        for key in keys
        if len({repr(f.get(key)) for f in fields}) > 1
    }


def _decision(
    doc: DocumentFacts,
    outcome_code: str,
    path: Path,
    *notes: str,
    is_duplicate: bool = False,
    duplicate_of: str | None = None,
    superseded: bool = False,
    **extra: Any,
) -> DocumentDecision:
    reason: dict[str, Any] = {"path": path.value, "notes": list(notes)}
    if doc.rule is not None:
        reason["rule_outcome"] = doc.rule.outcome_code
        reason["rule_reason"] = doc.rule.reason
        reason["rules_version"] = doc.rule.rules_version
    reason.update({k: v for k, v in extra.items() if v is not None})
    return DocumentDecision(
        document_id=doc.document_id,
        outcome_code=outcome_code,
        path=path,
        is_duplicate=is_duplicate,
        duplicate_of_document_id=duplicate_of,
        superseded_by_cutoff_rule=superseded,
        reason=reason,
    )


def _summary(decisions: Sequence[DocumentDecision]) -> dict[str, Any]:
    """``driver_evaluation.decision_summary``: counts and ids only, no clinical text."""
    paths = Counter(d.path.value for d in decisions)
    return {
        "document_count": len(decisions),
        "rejected_count": sum(d.is_duplicate for d in decisions),
        "cut_off_count": paths[Path.ALL_CUT_OFF] + paths[Path.CUT_OFF_SUPERSEDED],
        "attached_report_count": paths[Path.ATTACHED_REPORT],
        "outcome_counts": dict(Counter(d.outcome_code for d in decisions)),
        "paths": dict(paths),
        "retained_document_ids": [
            d.document_id
            for d in decisions
            if d.path in (Path.DUPLICATE_RETAINED, Path.SINGLE_DOCUMENT)
        ],
    }
