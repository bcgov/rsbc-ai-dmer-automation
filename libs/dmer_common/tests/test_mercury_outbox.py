"""Post-Processing's outbox rows (pure builders, no database)."""

from dmer_common.db.mercury_outbox import (
    AI_COULD_NOT_PROCESS,
    OPERATION_ORDER,
    OutboxOperation,
    ProposedDriver,
    decision_comment,
    decision_rows,
    fallback_comment,
    fallback_rows,
    idempotency_key,
)

_O = OutboxOperation
DOC, GUID, DECISION = "doc-1", "guid-1", "decision-1"


def _rows(**overrides):
    args = {
        "decision_id": DECISION,
        "document_id": DOC,
        "document_guid": GUID,
        "outcome_code": "PU",
        "decided_by": "AI",
        "reason": {
            "path": "single_document",
            "notes": [],
            "rule_reason": "FAKE reason",
        },
        "mercury_driver_id": "M-7",
    }
    return decision_rows(**{**args, **overrides})


def test_every_ai_decision_queues_create_case_then_update_outcome():
    rows = _rows()
    assert [r.operation for r in rows] == [_O.CREATE_CASE, _O.UPDATE_OUTCOME]
    case, outcome = rows
    assert case.payload["mercury_driver_id"] == "M-7"
    assert outcome.payload["outcome_code"] == "PU"
    assert outcome.payload["decided_by"] == "AI"
    assert {r.document_id for r in rows} == {DOC}
    assert all(r.payload["document_guid"] == GUID for r in rows)


def test_reason_is_in_every_operations_comment():
    # I-13: in all cases the reason is mentioned in the comment.
    rows = _rows(
        reason={
            "path": "duplicate_rejected",
            "notes": ["Driver matched from the licence."],
            "rule_reason": "FAKE reason",
        },
        is_duplicate=True,
        duplicate_of_document_guid="guid-0",
        proposed_driver=True,
        proposed_mercury_driver_id="M-8",
    )
    assert {r.payload["comment"] for r in rows} == {
        "AI outcome: PU. Reason: FAKE reason. Driver matched from the licence."
    }


def test_duplicate_and_proposed_driver_add_their_operations_in_order():
    rows = _rows(
        is_duplicate=True,
        duplicate_of_document_guid="guid-0",
        proposed_driver=True,
        proposed_mercury_driver_id="M-8",
    )
    # A duplicate is flagged on its outcome row, not a MARK_DUPLICATE row.
    assert [r.operation for r in rows] == [
        op for op in OPERATION_ORDER if op is not _O.MARK_DUPLICATE
    ]
    by_op = {r.operation: r.payload for r in rows}
    assert by_op[_O.MAP_DRIVER]["mercury_driver_id"] == "M-8"
    assert by_op[_O.UPDATE_OUTCOME]["is_duplicate"] is True  # I-7
    assert by_op[_O.UPDATE_OUTCOME]["duplicate_of_document_guid"] == "guid-0"


def test_map_driver_carries_what_mercurys_add_driver_needs():
    (map_driver, *_) = _rows(
        proposed_driver=True,
        proposed_mercury_driver_id="M-8",
        proposed_driver_details=ProposedDriver(
            licence_number="01234567", first_name="FAKE", last_name="DRIVER"
        ),
    )
    assert map_driver.operation is _O.MAP_DRIVER
    assert map_driver.payload["licence_number"] == "01234567"
    assert map_driver.payload["first_name"] == "FAKE"
    assert map_driver.payload["last_name"] == "DRIVER"


def test_a_decision_that_is_not_a_duplicate_carries_no_duplicate_fields():
    outcome = _rows()[-1]
    assert "is_duplicate" not in outcome.payload
    assert "duplicate_of_document_guid" not in outcome.payload


def test_idempotency_keys_are_deterministic_and_distinct_per_operation():
    first, again = _rows(is_duplicate=True), _rows(is_duplicate=True)
    assert [r.idempotency_key for r in first] == [r.idempotency_key for r in again]
    assert len({r.idempotency_key for r in first}) == len(first)
    assert first[-1].idempotency_key == idempotency_key(DECISION, _O.UPDATE_OUTCOME)


def test_comment_uses_the_applied_rule_and_lists_differing_fields():
    comment = decision_comment(
        "IN",
        {
            "rule_reason": "own reason",
            "applied_rule": "highest-priority copy's reason",
            "differing_fields": {"opinion.yes": {}, "exam_date": {}},
            "notes": ["Waiting DMERs for this driver differ."],
        },
    )
    assert comment == (
        "AI outcome: IN. Reason: highest-priority copy's reason. "
        "Fields that differ: exam_date, opinion.yes. "
        "Waiting DMERs for this driver differ."
    )


def test_comment_without_a_reason_still_states_the_outcome():
    assert decision_comment("CP", None) == "AI outcome: CP."


def test_fallback_is_a_single_update_outcome_in_with_ai_could_not_process():
    (row,) = fallback_rows(
        source_id="doc-1:FALLBACK",
        document_id=DOC,
        document_guid=GUID,
        comment=fallback_comment("Mercury could not confirm the driver's DMERs."),
    )
    assert row.operation is _O.UPDATE_OUTCOME
    assert row.payload == {
        "document_guid": GUID,
        "comment": f"{AI_COULD_NOT_PROCESS}. Mercury could not confirm the driver's DMERs.",
        "outcome_code": "IN",
        "decided_by": "FALLBACK",
    }
    assert row.idempotency_key == "doc-1:FALLBACK:UPDATE_OUTCOME"


def test_fallback_comment_skips_blank_reasons():
    assert fallback_comment("", "  ", "Reason.") == f"{AI_COULD_NOT_PROCESS}. Reason."
    assert fallback_comment() == f"{AI_COULD_NOT_PROCESS}."
