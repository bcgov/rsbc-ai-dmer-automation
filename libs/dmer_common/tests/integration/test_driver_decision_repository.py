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
                "VALUES (:d, '2026-06-12', :c, :reason, :p, now())"
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
                text("INSERT INTO rules_version (version) VALUES ('2026-06-12')")
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
            assert older.rules_version == "2026-06-12"
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
                    evaluation_id,
                    runs,
                    summary={"document_count": 2},
                    now=NOW,
                    batch_document_ids=docs,
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
                # Post-Processing: decided documents are being posted
                assert set(rows) == {("POSTING", "POST")}
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
                await repo.record_decided(
                    evaluation_id, runs, summary={}, now=NOW, batch_document_ids=docs
                )
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
                    await repo.record_decided(
                        evaluation_id,
                        runs,
                        summary={},
                        now=NOW,
                        batch_document_ids=docs,
                    )
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


def test_record_decided_rejects_a_decision_outside_the_batch():
    from dmer_common.db.driver_decision import DecisionRecord

    async def run():
        async with (
            database() as (engine, driver_key, evaluation_id, docs),
            driver_decision_session(engine, driver_key) as repo,
        ):
            await repo.move_evaluation(
                evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
            )
            runs = await repo.start_decision(evaluation_id, docs, now=NOW)
            stray = DecisionRecord(document_id=str(uuid4()), outcome_code="IN")
            with pytest.raises(DriverDecisionStateError):
                await repo.record_decided(
                    evaluation_id,
                    runs,
                    summary={},
                    now=NOW,
                    batch_document_ids=docs,
                    decisions=(stray,),
                )
            assert (await repo.open_evaluation()).status is _E.EVALUATING

    asyncio.run(run())


def test_driver_licence_for_the_mercury_lookup():
    async def run():
        async with (
            database() as (engine, driver_key, _evaluation_id, _docs),
            driver_decision_session(engine, driver_key) as repo,
        ):
            assert await repo.driver_licence() == "01234567"

    asyncio.run(run())


def test_deciding_closes_the_batch():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                await repo.record_decided(
                    evaluation_id, runs, summary={}, now=NOW, batch_document_ids=docs
                )
                # the driver's next DMER starts a new batch
                assert await repo.open_evaluation() is None
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT status, open FROM driver_evaluation WHERE id = :e"
                        ),
                        {"e": evaluation_id},
                    )
                ).one()
            assert tuple(row) == ("DECIDED", False)

    asyncio.run(run())


def test_a_document_counted_meanwhile_rolls_the_decision_back():
    from dmer_common.db.driver_decision import BatchChangedError

    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                # the Rule Engine finishes another of the driver's DMERs now
                async with engine.begin() as other:
                    await _add_document(other, driver_key, evaluation_id, day=7)
                with pytest.raises(BatchChangedError):
                    await repo.record_decided(
                        evaluation_id,
                        runs,
                        summary={},
                        now=NOW,
                        batch_document_ids=docs,
                    )
                evaluation = await repo.open_evaluation()
            # nothing written: still open and EVALUATING, documents untouched
            assert evaluation.status is _E.EVALUATING
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


def test_route_batch_to_manual_review_gives_every_undecided_document_a_fallback():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            awaiting, not_signalled = docs
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "UPDATE dmer_document SET pipeline_status = 'RULES_APPLIED' "
                        "WHERE id = :d"
                    ),
                    {"d": not_signalled},
                )
                # an already-failed batch document keeps its own record
                failed = await _add_document(conn, driver_key, evaluation_id, day=7)
                await conn.execute(
                    text(
                        "UPDATE dmer_document SET pipeline_status = 'MANUAL_REVIEW' "
                        "WHERE id = :d"
                    ),
                    {"d": failed},
                )
            async with driver_decision_session(engine, driver_key) as repo:
                routed = await repo.route_batch_to_manual_review(
                    evaluation_id,
                    reason_code="DecisionGateway:MERCURY_UNVERIFIED",
                    message="fixed description",
                    note="FAKE note for Intake",
                    summary={"path": "fallback"},
                    now=NOW,
                )
                assert await repo.open_evaluation() is None  # closed
            assert sorted(routed) == sorted([awaiting, not_signalled])
            async with engine.connect() as conn:
                evaluation = (
                    await conn.execute(
                        text(
                            "SELECT status, open, decision_summary "
                            "FROM driver_evaluation WHERE id = :e"
                        ),
                        {"e": evaluation_id},
                    )
                ).one()
                statuses = dict(
                    (
                        await conn.execute(
                            text("SELECT id::text, pipeline_status FROM dmer_document")
                        )
                    ).all()
                )
                errors = (
                    await conn.execute(
                        text(
                            "SELECT document_id::text, stage::text, "
                            "failure_category::text, reason_code FROM processing_error"
                        )
                    )
                ).all()
                decisions = (
                    await conn.execute(
                        text(
                            "SELECT document_id::text, outcome_code, decided_by, "
                            "decision_reason FROM dmer_decision"
                        )
                    )
                ).all()
            assert tuple(evaluation) == ("DECIDED", False, {"path": "fallback"})
            assert statuses[awaiting] == statuses[not_signalled] == "MANUAL_REVIEW"
            assert {e.document_id for e in errors} == {awaiting, not_signalled}
            assert {(e.stage, e.failure_category, e.reason_code) for e in errors} == {
                ("DECISION", "PROCESSING", "DecisionGateway:MERCURY_UNVERIFIED")
            }
            assert {d.document_id for d in decisions} == {awaiting, not_signalled}
            assert {(d.outcome_code, d.decided_by) for d in decisions} == {
                ("IN", "FALLBACK")
            }
            assert decisions[0].decision_reason["notes"] == ["FAKE note for Intake"]
            # Post-Processing: each fallback reaches Mercury as IN with the note
            outbox = await _outbox(engine)
            assert {o.document_id for o in outbox} == {awaiting, not_signalled}
            assert {o.operation for o in outbox} == {"UPDATE_OUTCOME"}
            for o in outbox:
                assert o.payload["outcome_code"] == "IN"
                assert o.payload["decided_by"] == "FALLBACK"
                assert o.payload["comment"] == (
                    "AI could not process. FAKE note for Intake"
                )
                assert (o.status, o.attempt_count, o.next_attempt_at) == (
                    "PENDING",
                    0,
                    NOW,
                )

    asyncio.run(run())


async def _outbox(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT document_id::text, operation::text, payload, "
                    "idempotency_key, status::text, attempt_count, next_attempt_at "
                    "FROM mercury_outbox ORDER BY document_id, operation"
                )
            )
        ).all()


def test_decisions_and_their_outbox_rows_commit_together():
    from dmer_common.db.driver_decision import DecisionRecord

    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            older, newer = docs
            async with engine.begin() as conn:
                await conn.execute(text("UPDATE driver SET mercury_driver_id = 'M-7'"))
                guids = dict(
                    (
                        await conn.execute(
                            text(
                                "SELECT id::text, document_guid::text FROM dmer_document"
                            )
                        )
                    ).all()
                )
            decisions = (
                DecisionRecord(
                    document_id=newer,
                    outcome_code="CP",
                    proposed_driver_key=driver_key,
                    decision_reason={
                        "path": "duplicate_retained",
                        "notes": [],
                        "rule_reason": "FAKE reason CP",
                    },
                ),
                DecisionRecord(
                    document_id=older,
                    outcome_code="CP",
                    is_duplicate=True,
                    duplicate_of_document_id=newer,
                    decision_reason={
                        "path": "duplicate_rejected",
                        "notes": [],
                        "rule_reason": "FAKE reason CP",
                    },
                ),
            )
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                await repo.record_decided(
                    evaluation_id,
                    runs,
                    summary={},
                    now=NOW,
                    batch_document_ids=docs,
                    decisions=decisions,
                )
            outbox = await _outbox(engine)
            ops = {(o.document_id, o.operation) for o in outbox}
            assert ops == {
                (newer, "MAP_DRIVER"),
                (newer, "CREATE_CASE"),
                (newer, "UPDATE_OUTCOME"),
                (older, "CREATE_CASE"),
                (older, "MARK_DUPLICATE"),
                (older, "UPDATE_OUTCOME"),
            }
            by_op = {(o.document_id, o.operation): o.payload for o in outbox}
            assert by_op[(newer, "MAP_DRIVER")]["mercury_driver_id"] == "M-7"
            assert by_op[(older, "CREATE_CASE")]["mercury_driver_id"] == "M-7"
            assert by_op[(older, "MARK_DUPLICATE")] == {
                "document_guid": guids[older],
                "comment": "AI outcome: CP. Reason: FAKE reason CP.",
                "status": "Rejected",
                "duplicate_of_document_guid": guids[newer],
            }
            assert by_op[(newer, "UPDATE_OUTCOME")]["outcome_code"] == "CP"
            assert {o.status for o in outbox} == {"PENDING"}
            async with engine.connect() as conn:
                decision_ids = dict(
                    (
                        await conn.execute(
                            text(
                                "SELECT document_id::text, id::text FROM dmer_decision"
                            )
                        )
                    ).all()
                )
            # keyed on the decision, so the same decision can't be queued twice
            assert {o.idempotency_key for o in outbox} == {
                f"{decision_ids[d]}:{op}" for d, op in ops
            }

    asyncio.run(run())


def test_a_failed_decision_queues_no_outbox_rows():
    from dmer_common.db.driver_decision import BatchChangedError, DecisionRecord

    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            async with driver_decision_session(engine, driver_key) as repo:
                await repo.move_evaluation(
                    evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
                )
                runs = await repo.start_decision(evaluation_id, docs, now=NOW)
                async with engine.begin() as other:
                    await _add_document(other, driver_key, evaluation_id, day=7)
                with pytest.raises(BatchChangedError):
                    await repo.record_decided(
                        evaluation_id,
                        runs,
                        summary={},
                        now=NOW,
                        batch_document_ids=docs,
                        decisions=tuple(
                            DecisionRecord(document_id=d, outcome_code="IN")
                            for d in docs
                        ),
                    )
            # the decision and its outbox rows roll back together
            assert await _outbox(engine) == []

    asyncio.run(run())


def test_route_batch_to_manual_review_only_from_waiting():
    async def run():
        async with (
            database() as (engine, driver_key, evaluation_id, docs),
            driver_decision_session(engine, driver_key) as repo,
        ):
            await repo.move_evaluation(
                evaluation_id, expected=_E.WAITING, target=_E.READY, now=NOW
            )
            await repo.start_decision(evaluation_id, docs, now=NOW)  # EVALUATING
            with pytest.raises(DriverDecisionStateError):
                await repo.route_batch_to_manual_review(
                    evaluation_id,
                    reason_code="X:Y",
                    message="m",
                    note="n",
                    summary={},
                    now=NOW,
                )
            assert (await repo.open_evaluation()).status is _E.EVALUATING

    asyncio.run(run())
