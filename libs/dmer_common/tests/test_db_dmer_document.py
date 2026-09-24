"""Unit tests for the pipeline status state machine and dmer_document guard.

Behaviour specs (GIVEN/WHEN/THEN) for allowed/illegal ``pipeline_status``
transitions and the repository rejecting an illegal transition before any SQL
runs. Live-PostgreSQL coverage is provided by the integration test.
"""

from __future__ import annotations

import asyncio
from itertools import pairwise

import pytest
from dmer_common.db import (
    PipelineStage,
    PipelineStatus,
    is_valid_transition,
    next_statuses,
)
from dmer_common.db.dmer_document import (
    DmerDocumentRepository,
    InvalidStatusTransition,
    _validated_status,
)


def test_happy_path_transitions_are_valid():
    # GIVEN the ordered lifecycle
    order = [
        PipelineStatus.RECEIVED,
        PipelineStatus.DOWNLOADED,
        PipelineStatus.EXTRACTING,
        PipelineStatus.EXTRACTED,
        PipelineStatus.NORMALIZED,
        PipelineStatus.RULES_APPLIED,
        PipelineStatus.AWAITING_DRIVER_COMPLETION,
        PipelineStatus.DECIDED,
        PipelineStatus.POSTING,
        PipelineStatus.COMPLETED,
    ]
    # WHEN moving through each adjacent pair THEN each transition is valid
    for current, target in pairwise(order):
        assert is_valid_transition(current, target)


def test_manual_review_reachable_from_any_non_terminal_status():
    # GIVEN every non-terminal status
    for status in PipelineStatus:
        if status in (PipelineStatus.COMPLETED, PipelineStatus.MANUAL_REVIEW):
            continue
        # THEN it can transition to MANUAL_REVIEW
        assert PipelineStatus.MANUAL_REVIEW in next_statuses(status)


def test_terminal_statuses_have_no_successors():
    # GIVEN terminal statuses THEN nothing is reachable from them
    assert next_statuses(PipelineStatus.COMPLETED) == set()
    assert next_statuses(PipelineStatus.MANUAL_REVIEW) == set()


def test_skipping_a_stage_is_invalid():
    # GIVEN received WHEN jumping straight to extracted THEN it is not allowed
    assert not is_valid_transition(PipelineStatus.RECEIVED, PipelineStatus.EXTRACTED)


def test_validated_status_requires_received_on_insert():
    # GIVEN no existing row (current is None) WHEN target != RECEIVED THEN raises
    with pytest.raises(InvalidStatusTransition):
        _validated_status(None, PipelineStatus.EXTRACTING)
    # AND RECEIVED is accepted
    assert _validated_status(None, PipelineStatus.RECEIVED) is PipelineStatus.RECEIVED


def test_validated_status_allows_idempotent_same_status():
    # GIVEN a current status equal to the target THEN it passes (idempotent)
    assert (
        _validated_status(PipelineStatus.EXTRACTING, PipelineStatus.EXTRACTING)
        is PipelineStatus.EXTRACTING
    )


class _StubEngine:
    """Engine stand-in whose connect()/begin() fail if actually used."""

    def connect(self):  # pragma: no cover - should never be reached
        raise AssertionError("SQL should not run for an illegal transition")

    def begin(self):  # pragma: no cover
        raise AssertionError("SQL should not run for an illegal transition")


def test_repository_rejects_illegal_transition_before_sql(monkeypatch):
    # GIVEN a caller that expects the row to be at 'RECEIVED'
    repo = DmerDocumentRepository(_StubEngine())

    # WHEN it asks for a move that skips stages THEN it raises before any SQL
    async def run():
        await repo.upsert_status(
            "doc-1",
            "case-1",
            PipelineStatus.EXTRACTED,
            expected=PipelineStatus.RECEIVED,
        )

    with pytest.raises(InvalidStatusTransition):
        asyncio.run(run())


def test_repository_requires_document_guid_on_initial_insert(monkeypatch):
    # GIVEN an initial insert (expected=None)
    repo = DmerDocumentRepository(_StubEngine())

    # WHEN inserting RECEIVED without a document_guid THEN it raises
    async def run():
        await repo.upsert_status(
            "doc-1",
            "case-1",
            PipelineStatus.RECEIVED,
            expected=None,
            stage=PipelineStage.INGEST,
        )

    with pytest.raises(ValueError, match="document_guid"):
        asyncio.run(run())


def test_repository_requires_stage_on_initial_insert(monkeypatch):
    # GIVEN an initial insert (expected=None)
    repo = DmerDocumentRepository(_StubEngine())

    # WHEN inserting RECEIVED without a stage THEN it raises before any SQL
    async def run():
        await repo.upsert_status(
            "doc-1",
            "case-1",
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid="g-1",
        )

    with pytest.raises(ValueError, match="stage"):
        asyncio.run(run())
