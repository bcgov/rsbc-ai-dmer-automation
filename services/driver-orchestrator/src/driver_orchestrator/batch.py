"""Turn the driver's batch, as loaded from the database, into decision inputs."""

from __future__ import annotations

from dmer_common.db.driver_decision import BatchDocument

from .decision import DocumentFacts, RuleOutcome


def to_facts(doc: BatchDocument, *, driver_key: str) -> DocumentFacts:
    """One :class:`BatchDocument` as :func:`decision.decide` input.

    A document without a complete rule evaluation (no outcome code or rank)
    gets ``rule=None``, which the gateway sends to Intake rather than guessing.
    """
    rule = None
    if doc.outcome_code is not None and doc.priority_rank is not None:
        rule = RuleOutcome(
            outcome_code=doc.outcome_code,
            priority_rank=doc.priority_rank,
            reason=doc.outcome_reason,
            rules_version=doc.rules_version,
        )
    return DocumentFacts(
        document_id=doc.document_id,
        document_guid=doc.document_guid,
        received_date=doc.received_date,
        page_count=doc.page_count,
        is_cutoff=doc.is_cutoff,
        comparison_hash=doc.comparison_hash,
        comparison_fields=doc.comparison_fields,
        rule=rule,
        driver_key=driver_key,
        driver_resolved_by=doc.driver_resolved_by,
    )
