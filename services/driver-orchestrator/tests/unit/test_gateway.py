"""Unit tests for the Decision Gateway outcome logic (business sign-off rules)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from driver_orchestrator.decision import (
    DocumentFacts,
    Path,
    RuleOutcome,
    decide,
)

RANK = {"CP": 0, "IN": 1, "PR": 2, "PU": 3}


def _doc(
    doc_id: str,
    *,
    day: int = 1,
    outcome: str | None = "CP",
    hash_: str | None = "h-same",
    cutoff: bool | None = False,
    pages: int | None = 2,
    fields: dict | None = None,
) -> DocumentFacts:
    rule = (
        None
        if outcome is None
        else RuleOutcome(outcome, RANK[outcome], f"FAKE reason {outcome}", "rules-v1")
    )
    return DocumentFacts(
        document_id=doc_id,
        document_guid=f"guid-{doc_id}",
        received_date=datetime(2026, 10, day, tzinfo=UTC),
        page_count=pages,
        is_cutoff=cutoff,
        comparison_hash=hash_,
        comparison_fields=fields,
        rule=rule,
    )


def _by_id(batch):
    return {d.document_id: d for d in batch.decisions}


# --- single document -----------------------------------------------------------


@pytest.mark.parametrize("outcome", ["CP", "IN", "PR", "PU"])
def test_single_document_keeps_rule_outcome(outcome):
    batch = decide([_doc("a", outcome=outcome)])
    (d,) = batch.decisions
    assert (d.outcome_code, d.path, d.is_duplicate) == (
        outcome,
        Path.SINGLE_DOCUMENT,
        False,
    )
    assert d.reason["rule_reason"] == f"FAKE reason {outcome}"
    assert batch.summary["retained_document_ids"] == ["a"]


def test_empty_batch_is_an_error():
    with pytest.raises(ValueError):
        decide([])


# --- attached reports -----------------------------------------------------------


def test_more_than_two_pages_is_in_and_never_a_duplicate():
    # GIVEN the same form twice, one with a medical report attached
    batch = decide([_doc("a", day=1), _doc("b", day=2, pages=5)])
    d = _by_id(batch)
    # THEN the report goes to a person, unmarked; the other is processed alone
    assert (d["b"].outcome_code, d["b"].path, d["b"].is_duplicate) == (
        "IN",
        Path.ATTACHED_REPORT,
        False,
    )
    assert (d["a"].outcome_code, d["a"].path) == ("CP", Path.SINGLE_DOCUMENT)
    assert batch.summary["attached_report_count"] == 1


def test_unknown_page_count_is_processed():
    # page_count is not recorded yet by extraction: None must not block
    (d,) = decide([_doc("a", pages=None)]).decisions
    assert d.path is Path.SINGLE_DOCUMENT


# --- cut-off --------------------------------------------------------------------


def test_every_copy_cut_off_goes_to_a_person_with_a_note():
    batch = decide([_doc("a", cutoff=True), _doc("b", day=2, cutoff=True)])
    for d in batch.decisions:
        assert (d.outcome_code, d.path) == ("IN", Path.ALL_CUT_OFF)
        assert "clearer copy" in d.reason["notes"][0]


def test_unknown_cutoff_state_counts_as_cut_off():
    (d,) = decide([_doc("a", cutoff=None)]).decisions
    assert (d.outcome_code, d.path) == ("IN", Path.ALL_CUT_OFF)


def test_newer_cut_off_never_beats_older_clear_copy_on_clean_pass():
    # GIVEN an older clear CP copy and a newer cut-off copy
    batch = decide(
        [_doc("old-clear", day=1), _doc("new-cut", day=5, cutoff=True, hash_="h2")]
    )
    d = _by_id(batch)
    # THEN the clear copy is kept and the cut-off copy is rejected as its duplicate
    assert (d["old-clear"].outcome_code, d["old-clear"].path) == (
        "CP",
        Path.DUPLICATE_RETAINED,
    )
    cut = d["new-cut"]
    assert (cut.path, cut.is_duplicate, cut.superseded_by_cutoff_rule) == (
        Path.CUT_OFF_SUPERSEDED,
        True,
        True,
    )
    assert cut.duplicate_of_document_id == "old-clear"


def test_cut_off_copy_is_not_rejected_when_not_a_clean_pass():
    batch = decide([_doc("clear", outcome="PR"), _doc("cut", day=2, cutoff=True)])
    d = _by_id(batch)
    assert d["clear"].outcome_code == "PR"
    assert (d["cut"].outcome_code, d["cut"].is_duplicate) == ("PR", False)
    assert d["cut"].superseded_by_cutoff_rule is True


def test_cut_off_copy_outcome_is_ignored():
    # The cut-off copy's own rule outcome (read from a partial page) does not count
    batch = decide(
        [_doc("clear", outcome="CP"), _doc("cut", day=2, cutoff=True, outcome="PU")]
    )
    assert {d.outcome_code for d in batch.decisions} == {"CP"}
    assert _by_id(batch)["cut"].is_duplicate is True


# --- duplicates -----------------------------------------------------------------


def test_clean_pass_duplicates_keep_newest_and_reject_older():
    batch = decide([_doc("old", day=1), _doc("mid", day=2), _doc("new", day=3)])
    d = _by_id(batch)
    assert d["new"].path is Path.DUPLICATE_RETAINED and not d["new"].is_duplicate
    for older in ("old", "mid"):
        assert d[older].path is Path.DUPLICATE_REJECTED
        assert (d[older].is_duplicate, d[older].duplicate_of_document_id) == (
            True,
            "new",
        )
    assert batch.summary["rejected_count"] == 2
    assert batch.summary["retained_document_ids"] == ["new"]


def test_non_clean_pass_duplicates_are_all_forwarded():
    batch = decide([_doc("old", day=1, outcome="PU"), _doc("new", day=2, outcome="PU")])
    for d in batch.decisions:
        assert (d.outcome_code, d.is_duplicate) == ("PU", False)
        assert d.reason["duplicate_group"] == ["new", "old"]
    assert batch.summary["rejected_count"] == 0


def test_duplicates_with_disagreeing_outcomes_take_highest_priority_and_reject_nothing():
    # Same hash, but the rule engine (which also reads handwriting) disagreed
    batch = decide([_doc("old", day=1, outcome="CP"), _doc("new", day=2, outcome="PR")])
    for d in batch.decisions:
        assert (d.outcome_code, d.is_duplicate) == ("PR", False)
        assert "different rule engine outcomes" in d.reason["notes"][0]
        assert d.reason["applied_rule"] == "FAKE reason PR"


def test_newest_tie_broken_by_document_guid():
    batch = decide([_doc("a", day=1), _doc("b", day=1)])
    assert _by_id(batch)["b"].path is Path.DUPLICATE_RETAINED


def test_missing_received_date_counts_as_oldest():
    undated = _doc("undated", day=9)
    undated = DocumentFacts(**{**undated.__dict__, "received_date": None})
    batch = decide([undated, _doc("dated", day=1)])
    assert _by_id(batch)["dated"].path is Path.DUPLICATE_RETAINED


# --- differing content ----------------------------------------------------------


def test_differing_content_sends_every_copy_to_a_person_with_the_diff():
    a = _doc(
        "a",
        hash_="h1",
        fields={"version": 1, "opinion.yes": True, "cns.dementia": False},
    )
    b = _doc(
        "b",
        day=2,
        hash_="h2",
        fields={"version": 1, "opinion.yes": False, "cns.dementia": False},
    )
    batch = decide([a, b])
    for d in batch.decisions:
        assert (d.outcome_code, d.path, d.is_duplicate) == (
            "IN",
            Path.DIFFERING_CONTENT,
            False,
        )
        assert d.reason["differing_fields"] == {"opinion.yes": {"b": False, "a": True}}


def test_missing_hash_is_never_a_duplicate():
    batch = decide([_doc("a", hash_=None), _doc("b", day=2, hash_=None)])
    assert {d.path for d in batch.decisions} == {Path.DIFFERING_CONTENT}


def test_differing_content_with_a_cut_off_copy():
    batch = decide(
        [
            _doc("a", hash_="h1"),
            _doc("b", day=2, hash_="h2"),
            _doc("c", day=3, cutoff=True),
        ]
    )
    assert {d.outcome_code for d in batch.decisions} == {"IN"}
    assert _by_id(batch)["c"].superseded_by_cutoff_rule is True


# --- safety ---------------------------------------------------------------------


def test_missing_rule_outcome_goes_to_a_person():
    batch = decide([_doc("a", outcome=None), _doc("b", day=2)])
    d = _by_id(batch)
    assert (d["a"].outcome_code, d["a"].path) == ("IN", Path.NO_RULE_OUTCOME)
    assert d["b"].path is Path.SINGLE_DOCUMENT


def test_every_document_gets_exactly_one_decision_in_input_order():
    docs = [_doc("x", day=3), _doc("y", day=1, pages=4), _doc("z", day=2, cutoff=True)]
    batch = decide(docs)
    assert [d.document_id for d in batch.decisions] == ["x", "y", "z"]


def test_summary_has_no_clinical_text():
    batch = decide([_doc("a", outcome="PU"), _doc("b", day=2, outcome="PU")])
    assert "FAKE reason" not in repr(batch.summary)
    assert batch.summary["outcome_counts"] == {"PU": 2}


# --- driver mapping (I-11) ------------------------------------------------------


def _with_driver(doc, resolved_by):
    return DocumentFacts(
        **{**doc.__dict__, "driver_key": "driver-1", "driver_resolved_by": resolved_by}
    )


def test_driver_matched_from_licence_is_proposed_for_mapping():
    (d,) = decide([_with_driver(_doc("a"), "LICENCE_LOOKUP")]).decisions
    assert (d.driver_mapped, d.proposed_driver_key) == (False, "driver-1")
    assert any("mapping to the driver's file proposed" in n for n in d.reason["notes"])


def test_mercury_supplied_driver_is_already_mapped():
    (d,) = decide([_with_driver(_doc("a"), "MERCURY_SUPPLIED")]).decisions
    assert (d.driver_mapped, d.proposed_driver_key) == (True, None)


def test_mapping_is_proposed_on_every_path():
    batch = decide(
        [
            _with_driver(_doc("old", day=1), "LICENCE_LOOKUP"),
            _with_driver(_doc("new", day=2), "LICENCE_LOOKUP"),
        ]
    )
    assert {d.proposed_driver_key for d in batch.decisions} == {"driver-1"}
    assert batch.summary["driver_mapping_proposed_count"] == 2
