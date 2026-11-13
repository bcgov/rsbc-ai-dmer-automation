"""Unit tests for the Decision Gateway completeness check (step 1)."""

from __future__ import annotations

import pytest
from driver_orchestrator.decision.completeness import (
    BatchMember,
    WaitReason,
    check_completeness,
    mercury_waiting_count,
)

AWAITING = "AWAITING_DRIVER_COMPLETION"


def _doc(type_="DMER", status="Uploaded", dps_date=""):
    return {"document_type": type_, "document_status": status, "dps_date": dps_date}


def _batch(*statuses):
    return [BatchMember(f"doc-{i}", s) for i, s in enumerate(statuses)]


# --- counting Mercury's waiting DMERs -----------------------------------------


def test_counts_untriaged_dmers_only():
    driver = {
        "active_documents": [
            _doc(),
            _doc(type_=" dmer "),  # case and spacing
            _doc(status="Rejected"),  # I-8: does not hold the batch up
            _doc(dps_date="2026-10-01T00:00:00Z"),  # already triaged (I-9)
            _doc(type_="Test Report"),  # not a DMER
        ]
    }
    assert mercury_waiting_count([driver]) == 2


def test_missing_or_null_dps_date_counts_as_untriaged():
    driver = {"active_documents": [{"document_type": "DMER"}, _doc(dps_date=None)]}
    assert mercury_waiting_count([driver]) == 2


@pytest.mark.parametrize("drivers", [[], [{}, {}]])
def test_no_driver_or_several_cannot_be_verified(drivers):
    assert mercury_waiting_count(drivers) is None


def test_no_active_documents_counts_zero():
    assert mercury_waiting_count([{}]) == 0


def test_counted_types_and_statuses_are_configurable():
    driver = {"active_documents": [_doc(type_="Report"), _doc(status="On Hold")]}
    assert (
        mercury_waiting_count(
            [driver], counted_types={"Report", "DMER"}, uncounted_statuses={"On Hold"}
        )
        == 1
    )


# --- is the batch ready? ------------------------------------------------------


def test_ready_when_every_waiting_dmer_is_awaiting_its_driver():
    result = check_completeness(2, _batch(AWAITING, AWAITING))
    assert result.ready and result.wait_reason is None
    assert result.to_decide == ("doc-0", "doc-1")
    assert (result.expected, result.done) == (2, 2)


def test_waits_for_documents_mercury_holds_but_we_have_not_finished():
    result = check_completeness(3, _batch(AWAITING, AWAITING))
    assert not result.ready
    assert result.wait_reason is WaitReason.WAITING_FOR_DOCUMENTS
    assert result.to_decide == ()


def test_waits_while_a_counted_document_is_not_yet_signalled():
    result = check_completeness(2, _batch(AWAITING, "RULES_APPLIED"))
    assert result.wait_reason is WaitReason.DOCUMENTS_STILL_PROCESSING


def test_waits_when_mercury_could_not_verify():
    result = check_completeness(None, _batch(AWAITING))
    assert result.wait_reason is WaitReason.MERCURY_UNVERIFIED


def test_manual_review_document_counts_as_finished_but_is_not_decided():
    result = check_completeness(2, _batch(AWAITING, "MANUAL_REVIEW"))
    assert result.ready
    assert result.to_decide == ("doc-0",)


def test_more_finished_than_mercury_expects_is_ready():
    # a DMER was triaged by a person meanwhile, so Mercury now expects fewer
    assert check_completeness(1, _batch(AWAITING, AWAITING)).ready


def test_every_document_in_manual_review_is_ready_with_nothing_to_decide():
    result = check_completeness(1, _batch("MANUAL_REVIEW"))
    assert result.ready and result.to_decide == ()


def test_empty_batch_waits_while_mercury_expects_documents():
    assert check_completeness(1, []).wait_reason is WaitReason.WAITING_FOR_DOCUMENTS


def test_accepts_database_batch_rows():
    from datetime import UTC, datetime

    from dmer_common.db.driver_decision import BatchDocument

    row = BatchDocument(
        document_id="doc-1",
        document_guid="guid-1",
        received_date=datetime(2026, 10, 1, tzinfo=UTC),
        pipeline_status=AWAITING,
        driver_resolved_by=None,
        is_cutoff=False,
        page_count=None,
        comparison_hash=None,
        comparison_fields=None,
        outcome_code="CP",
        priority_rank=0,
        outcome_reason=None,
        rules_version=None,
    )
    assert check_completeness(1, [row]).to_decide == ("doc-1",)
