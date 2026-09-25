"""Focused unit tests for Normalize DB unit-of-work behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from dmer_common.db.normalization import (
    NormalizationBusyError,
    NormalizationNotReadyError,
    NormalizationRepository,
    NormalizationStateError,
    normalization_session,
)


class _FakeResult:
    def __init__(self, *, rows: list[tuple] | None = None, scalar=None) -> None:
        self._rows = rows or []
        self._scalar = scalar

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        if self._scalar is not None:
            return self._scalar
        if len(self._rows) != 1 or len(self._rows[0]) != 1:
            raise AssertionError("scalar_one() expected exactly one scalar value")
        return self._rows[0][0]


class _FakeTx:
    def __init__(self, conn: _ScriptedConn) -> None:
        self._conn = conn

    async def __aenter__(self):
        self._conn.tx_begin_count += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.tx_commit_count += 1
        else:
            self._conn.tx_rollback_count += 1
        return False


class _ScriptedConn:
    def __init__(self, script: list) -> None:
        self._script = script
        self.executed_sql: list[str] = []
        self.tx_begin_count = 0
        self.tx_commit_count = 0
        self.tx_rollback_count = 0
        self.commit_count = 0
        self.close_count = 0
        self.invalidate_count = 0
        self.closed = False
        self.invalidated = False

    async def execute(self, stmt, params=None):
        if not self._script:
            raise AssertionError(f"unexpected execute call: {stmt!s}")
        self.executed_sql.append(str(stmt))
        step = self._script.pop(0)
        return step(stmt, params)

    def begin(self):
        return _FakeTx(self)

    async def commit(self):
        self.commit_count += 1

    async def close(self):
        self.close_count += 1

    async def invalidate(self):
        self.invalidate_count += 1

    def assert_consumed(self):
        assert self._script == []


class _FakeEngine:
    def __init__(self, conn: _ScriptedConn) -> None:
        self._conn = conn

    async def connect(self):
        return self._conn


def _now() -> datetime:
    return datetime(2026, 9, 24, 15, 30, tzinfo=UTC)


def test_normalization_session_busy_lock_raises_and_closes_connection():
    async def run():
        conn = _ScriptedConn([
            lambda stmt, params: _FakeResult(scalar=False),
        ])
        engine = _FakeEngine(conn)

        with pytest.raises(NormalizationBusyError):
            async with normalization_session(engine, "doc-1"):
                raise AssertionError("must not yield repository when lock is busy")

        conn.assert_consumed()
        assert conn.commit_count == 1  # commit lock-select transaction
        assert conn.close_count == 1

    asyncio.run(run())


def test_lost_database_session_cannot_reconnect_and_commit_without_lock():
    async def run():
        conn = _ScriptedConn([])
        conn.invalidated = True
        repo = NormalizationRepository(conn, document_id="doc-1")
        with pytest.raises(NormalizationBusyError):
            await repo.succeed(
                1, ended_at=_now(), output_blob_url="https://blob/normalized",
                model_version="model@v2",
            )
        assert conn.tx_begin_count == 0
        assert conn.executed_sql == []

    asyncio.run(run())


def test_start_or_resume_replays_persisted_success_for_later_status():
    async def run():
        conn = _ScriptedConn([
            lambda stmt, params: _FakeResult(rows=[("RULES_APPLIED",)]),
            lambda stmt, params: _FakeResult(rows=[(99, 4, "normalized-dmer/doc-1.json")]),
        ])
        repo = NormalizationRepository(conn, document_id="doc-1")

        run_info = await repo.start_or_resume(now=_now(), model_version="new-model@v2")

        conn.assert_consumed()
        assert run_info.run_id == 99
        assert run_info.attempt_no == 4
        assert run_info.output_blob_url == "normalized-dmer/doc-1.json"
        assert conn.tx_commit_count == 1

    asyncio.run(run())


def test_start_or_resume_extracted_starts_new_attempt_and_interrupts_old_running():
    async def run():
        conn = _ScriptedConn([
            lambda stmt, params: _FakeResult(rows=[("EXTRACTED",)]),
            lambda stmt, params: _FakeResult(),
            lambda stmt, params: _FakeResult(scalar=2),
            lambda stmt, params: _FakeResult(rows=[("doc-1",)]),
            lambda stmt, params: _FakeResult(rows=[(123,)]),
        ])
        repo = NormalizationRepository(conn, document_id="doc-1")

        run_info = await repo.start_or_resume(now=_now(), model_version="gpt-5.1@schema-v1")

        conn.assert_consumed()
        assert run_info.run_id == 123
        assert run_info.attempt_no == 3
        assert run_info.output_blob_url is None
        # First write in the new-attempt path marks stale RUNNING rows FAILED.
        assert "UPDATE dmer_stage_run" in conn.executed_sql[1]
        assert "dmer_stage_run.status =" in conn.executed_sql[1]

    asyncio.run(run())


def test_start_or_resume_not_ready_raises_without_extra_writes():
    async def run():
        conn = _ScriptedConn([
            lambda stmt, params: _FakeResult(rows=[("EXTRACTING",)]),
        ])
        repo = NormalizationRepository(conn, document_id="doc-1")

        with pytest.raises(NormalizationNotReadyError):
            await repo.start_or_resume(now=_now(), model_version="gpt-5.1@schema-v1")

        conn.assert_consumed()
        assert len(conn.executed_sql) == 1

    asyncio.run(run())


def test_succeed_rollback_when_document_update_condition_fails():
    async def run():
        conn = _ScriptedConn([
            lambda stmt, params: _FakeResult(rows=[(777,)]),
            lambda stmt, params: _FakeResult(rows=[]),
        ])
        repo = NormalizationRepository(conn, document_id="doc-1")

        with pytest.raises(NormalizationStateError):
            await repo.succeed(
                777,
                ended_at=_now(),
                output_blob_url="normalized-dmer/doc-1.json",
                model_version="gpt-5.1@schema-v1",
            )

        conn.assert_consumed()
        assert conn.tx_rollback_count == 1
        assert conn.tx_commit_count == 0

    asyncio.run(run())
