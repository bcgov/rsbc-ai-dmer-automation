"""Decision Gateway unit of work against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable local/CI PostgreSQL database (the
same variable the Normalize and Rule Engine repository tests use). Each test
creates and drops its own schema with the real migrations applied.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.driver_decision import (
    DriverDecisionBusyError,
    DriverDecisionStateError,
    EvaluationStatus,
    driver_decision_session,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
NOW = datetime(2026, 10, 8, tzinfo=UTC)
_E = EvaluationStatus


async def _scalar(conn, sql, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _add_document(
    conn, driver_key, evaluation_id, *, day, rules=None, counted=True
):
    """A document awaiting its driver, with extraction values and (optionally)
    two rule evaluations, the second one the latest."""
    doc = str(
        await _scalar(
            conn,
            "INSERT INTO dmer_document (document_guid, pipeline_status, current_stage, "
            "received_date, driver_key, driver_resolved_by) VALUES (gen_random_uuid(), "
            "'AWAITING_DRIVER_COMPLETION', 'DECISION', :r, :k, 'MERCURY_SUPPLIED') "
            "RETURNING id",
            r=datetime(2026, 10, day, tzinfo=UTC),
            k=driver_key,
        )
    )
    await conn.execute(
        text(
            "INSERT INTO dmer_extraction (document_id, is_cutoff, page_count, "
            "comparison_hash, comparison_fields) VALUES (:d, false, 2, :h, "
            "CAST(:f AS jsonb))"
        ),
        {"d": doc, "h": "a" * 64, "f": '{"version": 1, "opinion.yes": true}'},
    )
    for code, rank in rules or []:
        await conn.execute(
            text(
                "INSERT INTO rule_evaluation (document_id, rules_version, "
                "selected_outcome_code, selected_reason, priority_rank, evaluated_at) "
                "VALUES (:d, 'rules-v1', :c, :reason, :p, now())"
            ),
            {"d": doc, "c": code, "reason": f"FAKE reason {code}", "p": rank},
        )
    if counted:
        await conn.execute(
            text(
                "INSERT INTO driver_evaluation_document (driver_evaluation_id, "
                "document_id, counted_at) VALUES (:e, :d, now())"
            ),
            {"e": evaluation_id, "d": doc},
        )
    return doc


@asynccontextmanager
async def database():
    schema = "decision_test_" + uuid4().hex
    engine = create_async_engine(
        os.environ["NORMALIZATION_TEST_DSN"],
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            raw = await conn.get_raw_connection()
            for script in _SCHEMA:
                await raw.driver_connection.execute(script.read_text(encoding="utf-8"))
            await conn.execute(
                text("INSERT INTO rules_version (version) VALUES ('rules-v1')")
            )
            driver_key = str(
                await _scalar(
                    conn,
                    "INSERT INTO driver (licence_number) VALUES ('01234567') "
                    "RETURNING driver_key",
                )
            )
            evaluation_id = str(
                await _scalar(
                    conn,
                    "INSERT INTO driver_evaluation (driver_key, expected_document_count, "
                    "completed_document_count) VALUES (:k, 2, 2) RETURNING id",
                    k=driver_key,
                )
            )
            older = await _add_document(
                conn, driver_key, evaluation_id, day=1, rules=[("CP", 0), ("PU", 3)]
            )
            newer = await _add_document(conn, driver_key, evaluation_id, day=5)
            await _add_document(conn, driver_key, evaluation_id, day=9, counted=False)
        yield engine, driver_key, evaluation_id, [older, newer]
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def test_open_evaluation_and_batch_reads_back():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                evaluation = await repo.open_evaluation()
                assert evaluation.id == evaluation_id
                assert evaluation.status is _E.WAITING
                assert (
                    evaluation.expected_document_count,
                    evaluation.completed_document_count,
                ) == (2, 2)

                batch = await repo.load_batch(evaluation_id)
            # Only counted documents, oldest first
            assert [d.document_id for d in batch] == docs
            older, newer = batch
            # The latest rule evaluation wins (PU inserted after CP)
            assert (older.outcome_code, older.priority_rank) == ("PU", 3)
            assert older.outcome_reason == "FAKE reason PU"
            assert older.rules_version == "rules-v1"
            # No rule evaluation -> None, not an error
            assert newer.outcome_code is None and newer.priority_rank is None
            assert older.comparison_hash == "a" * 64
            assert older.comparison_fields == {"version": 1, "opinion.yes": True}
            assert (older.is_cutoff, older.page_count) == (False, 2)
            assert older.driver_resolved_by == "MERCURY_SUPPLIED"
            assert older.pipeline_status == "AWAITING_DRIVER_COMPLETION"

    asyncio.run(run())


def test_full_decision_moves_evaluation_documents_and_stage_runs():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id,
                    expected=_E.WAITING,
                    target=_E.READY,
                    now=NOW,
                    expected_document_count=2,
                    mercury_checked=True,
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                assert set(runs) == set(docs)
                await repo.record_decided(
                    evaluation_id, runs, summary={"document_count": 2}, now=NOW
                )
            async with engine.connect() as conn:
                status, summary, checked = (
                    await conn.execute(
                        text(
                            "SELECT status, decision_summary, last_mercury_check_at "
                            "FROM driver_evaluation WHERE id = :e"
                        ),
                        {"e": evaluation_id},
                    )
                ).one()
                assert (status, summary, checked) == (
                    "DECIDED",
                    {"document_count": 2},
                    NOW,
                )
                rows = (
                    await conn.execute(
                        text(
                            "SELECT pipeline_status, current_stage FROM dmer_document "
                            "WHERE id = ANY(CAST(:d AS uuid[]))"
                        ),
                        {"d": docs},
                    )
                ).all()
                assert set(rows) == {("DECIDED", "POST")}
                stages = (
                    await conn.execute(
                        text(
                            "SELECT status, attempt_no FROM dmer_stage_run "
                            "WHERE stage = 'DECISION'"
                        )
                    )
                ).all()
                assert sorted(stages) == [("SUCCEEDED", 1), ("SUCCEEDED", 1)]

    asyncio.run(run())


def test_retried_decision_closes_the_interrupted_attempt():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                await repo.start_decision(evaluation_id, docs, now=NOW)
                # crash, then a retry re-enters from EVALUATING
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                await repo.record_decided(evaluation_id, runs, summary={}, now=NOW)
            async with engine.connect() as conn:
                stages = (
                    await conn.execute(
                        text(
                            "SELECT status, attempt_no, error_code FROM dmer_stage_run "
                            "WHERE stage = 'DECISION' ORDER BY attempt_no"
                        )
                    )
                ).all()
            assert [s[:2] for s in stages] == [
                ("FAILED", 1),
                ("FAILED", 1),
                ("SUCCEEDED", 2),
                ("SUCCEEDED", 2),
            ]
            assert stages[0].error_code == "InterruptedAttempt"

    asyncio.run(run())


def test_failed_decision_goes_back_to_waiting():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                await repo.fail_decision(
                    evaluation_id, runs, error_code="Boom", now=NOW
                )
                assert (await repo.open_evaluation()).status is _E.WAITING
            async with engine.connect() as conn:
                statuses = (
                    (
                        await conn.execute(
                            text("SELECT DISTINCT pipeline_status FROM dmer_document")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert statuses == ["AWAITING_DRIVER_COMPLETION"]

    asyncio.run(run())


def test_record_decided_changes_nothing_if_a_document_moved_on():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                async with engine.begin() as other:
                    await other.execute(
                        text(
                            "UPDATE dmer_document SET pipeline_status = 'MANUAL_REVIEW' "
                            "WHERE id = :d"
                        ),
                        {"d": docs[0]},
                    )
                with pytest.raises(DriverDecisionStateError):
                    await repo.record_decided(evaluation_id, runs, summary={}, now=NOW)
                # rolled back: still EVALUATING, the other document untouched
                assert (await repo.open_evaluation()).status is _E.EVALUATING
            async with engine.connect() as conn:
                status = await _scalar(
                    conn,
                    "SELECT pipeline_status FROM dmer_document WHERE id = :d",
                    d=docs[1],
                )
            assert status == "AWAITING_DRIVER_COMPLETION"

    asyncio.run(run())


def test_stale_compare_and_set_raises():
    async def run():
        async with (
            database() as (engine, driver_key, evaluation_id, _),
            driver_decision_session(engine, driver_key) as repo,
        ):
            with pytest.raises(DriverDecisionStateError):
                await repo.move_evaluation(
                    evaluation_id, expected=_E.READY, target=_E.EVALUATING, now=NOW
                )
            with pytest.raises(DriverDecisionStateError):
                await repo.start_decision(evaluation_id, [], now=NOW)  # WAITING

    asyncio.run(run())


def test_second_session_for_the_same_driver_is_busy():
    async def run():
        async with (
            database() as (engine, driver_key, _, _docs),
            driver_decision_session(engine, driver_key),
        ):
            with pytest.raises(DriverDecisionBusyError):
                async with driver_decision_session(engine, driver_key):
                    pass

    asyncio.run(run())


def test_no_open_evaluation_returns_none():
    async def run():
        async with (
            database() as (engine, _, _evaluation_id, _docs),
            driver_decision_session(engine, str(uuid4())) as repo,
        ):
            assert await repo.open_evaluation() is None

    asyncio.run(run())
