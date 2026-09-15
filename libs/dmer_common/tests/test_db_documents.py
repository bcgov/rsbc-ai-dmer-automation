"""Unit tests for the document status state machine and repository guard.

Behaviour specs (GIVEN/WHEN/THEN) for allowed/illegal transitions and the
repository rejecting an illegal transition before any SQL runs. Live-PostgreSQL
coverage is provided by the integration test.
"""

from __future__ import annotations

import asyncio

import pytest
from dmer_common.db import DocumentStatus, is_valid_transition, next_statuses
from dmer_common.db.documents import (
    DocumentRepository,
    InvalidStatusTransition,
    _validated_status,
)


def test_happy_path_transitions_are_valid():
    # GIVEN the ordered lifecycle
    order = [
        DocumentStatus.RECEIVED,
        DocumentStatus.EXTRACTING,
        DocumentStatus.SECTIONING,
        DocumentStatus.COMBINING,
        DocumentStatus.COMBINED,
        DocumentStatus.PUBLISHED,
    ]
    # WHEN moving through each adjacent pair THEN each transition is valid
    from itertools import pairwise

    for current, target in pairwise(order):
        assert is_valid_transition(current, target)


def test_failed_reachable_from_any_non_terminal_status():
    # GIVEN every non-terminal status
    for status in DocumentStatus:
        if status in (DocumentStatus.PUBLISHED, DocumentStatus.FAILED):
            continue
        # THEN it can transition to failed
        assert DocumentStatus.FAILED in next_statuses(status)


def test_terminal_statuses_have_no_successors():
    # GIVEN terminal statuses
    # THEN nothing is reachable from them
    assert next_statuses(DocumentStatus.PUBLISHED) == set()
    assert next_statuses(DocumentStatus.FAILED) == set()


def test_skipping_a_stage_is_invalid():
    # GIVEN received
    # WHEN attempting to jump straight to combined THEN it is not allowed
    assert not is_valid_transition(DocumentStatus.RECEIVED, DocumentStatus.COMBINED)


def test_validated_status_requires_received_on_insert():
    # GIVEN no existing row (current is None)
    # WHEN the target is not received THEN it raises
    with pytest.raises(InvalidStatusTransition):
        _validated_status(None, DocumentStatus.EXTRACTING)
    # AND received is accepted
    assert _validated_status(None, DocumentStatus.RECEIVED) is DocumentStatus.RECEIVED


def test_validated_status_allows_idempotent_same_status():
    # GIVEN a current status equal to the target
    # WHEN validated THEN it passes (idempotent re-write)
    assert (
        _validated_status(DocumentStatus.EXTRACTING, DocumentStatus.EXTRACTING)
        is DocumentStatus.EXTRACTING
    )


class _StubEngine:
    """Engine stand-in whose connect() would fail if actually used."""

    def connect(self):  # pragma: no cover - should never be reached
        raise AssertionError("SQL should not run for an illegal transition")

    def begin(self):  # pragma: no cover
        raise AssertionError("SQL should not run for an illegal transition")


def test_repository_rejects_illegal_transition_before_sql(monkeypatch):
    # GIVEN a repository whose current status is 'received'
    repo = DocumentRepository(_StubEngine())

    async def fake_get_status(_doc_id):
        return DocumentStatus.RECEIVED

    monkeypatch.setattr(repo, "get_status", fake_get_status)

    # WHEN upserting a status that skips stages THEN it raises before any SQL
    async def run():
        await repo.upsert_status("doc-1", "case-1", DocumentStatus.PUBLISHED)

    with pytest.raises(InvalidStatusTransition):
        asyncio.run(run())
