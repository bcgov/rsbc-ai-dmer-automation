"""Unit tests for turning database batch rows into decision inputs."""

from __future__ import annotations

from datetime import UTC, datetime

from dmer_common.db.driver_decision import BatchDocument
from driver_orchestrator.batch import to_facts


def _row(**overrides) -> BatchDocument:
    values = {
        "document_id": "doc-1",
        "document_guid": "guid-1",
        "received_date": datetime(2026, 10, 1, tzinfo=UTC),
        "pipeline_status": "AWAITING_DRIVER_COMPLETION",
        "driver_resolved_by": "LICENCE_LOOKUP",
        "is_cutoff": False,
        "page_count": 2,
        "comparison_hash": "a" * 64,
        "comparison_fields": {"version": 1},
        "outcome_code": "PU",
        "priority_rank": 3,
        "outcome_reason": "FAKE reason",
        "rules_version": "rules-v1",
    }
    values.update(overrides)
    return BatchDocument(**values)


def test_every_field_is_carried_over():
    facts = to_facts(_row(), driver_key="driver-1")
    assert (facts.document_id, facts.document_guid) == ("doc-1", "guid-1")
    assert (facts.is_cutoff, facts.page_count) == (False, 2)
    assert facts.comparison_hash == "a" * 64
    assert (facts.driver_key, facts.driver_resolved_by) == (
        "driver-1",
        "LICENCE_LOOKUP",
    )
    assert (facts.rule.outcome_code, facts.rule.priority_rank) == ("PU", 3)
    assert (facts.rule.reason, facts.rule.rules_version) == ("FAKE reason", "rules-v1")


def test_missing_rule_evaluation_becomes_no_rule():
    assert (
        to_facts(_row(outcome_code=None, priority_rank=None), driver_key="d").rule
        is None
    )
    assert to_facts(_row(priority_rank=None), driver_key="d").rule is None
