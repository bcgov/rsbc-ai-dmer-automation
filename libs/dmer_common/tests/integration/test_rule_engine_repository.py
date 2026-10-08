"""Rule Engine unit of work against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable local/CI PostgreSQL database (the
same variable the Normalize repository tests use). Each test creates and drops
its own schema with the real migrations applied.
"""

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.rule_engine import (
    UNREGISTERED_ACTIVATED_BY,
    RuleEngineBusyError,
    RuleEngineNotReadyError,
    RuleEngineStateError,
    rule_engine_session,
)
from dmer_common.rules import Outcome, select_outcome
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
# Every migration, in version order -- the schema production runs, so a
# new column the code needs can never be missing here.
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
RECEIVED = datetime(2026, 9, 1, tzinfo=UTC)
CHECKSUM = hashlib.sha256(b"rules").hexdigest()
EVALUATION = select_outcome(
    (
        Outcome("Cardiovascular", "PR", False, "CAD with concerns"),
        Outcome("Vision", "CP", True, "vision meets standard"),
    )
)


@asynccontextmanager
async def database(*, status="NORMALIZED", with_driver=True, open_evaluation=True):
    schema = "rules_test_" + uuid4().hex
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
            driver_key = None
            if with_driver:
                driver_key = str(
                    (
                        await conn.execute(
                            text(
                                "INSERT INTO driver (licence_number) VALUES ('01234567') "
                                "RETURNING driver_key"
                            )
                        )
                    ).scalar_one()
                )
                if open_evaluation:
                    await conn.execute(
                        text(
                            "INSERT INTO driver_evaluation (driver_key, expected_document_count) "
                            "VALUES (:k, 2)"
                        ),
                        {"k": driver_key},
                    )
            document_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO dmer_document (document_guid, pipeline_status, "
                            "current_stage, received_date, driver_key) VALUES "
                            "(gen_random_uuid(), CAST(:s AS dmer_pipeline_status), 'RULES', :r, :k) "
                            "RETURNING id"
                        ),
                        {"s": status, "r": RECEIVED, "k": driver_key},
                    )
                ).scalar_one()
            )
        yield engine, document_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _evaluate(engine, document_id):
    """One full attempt: start, register the ruleset, succeed."""
    async with rule_engine_session(engine, document_id) as repository:
        run = await repository.start_or_resume(now=datetime.now(UTC))
        assert run.completed is None
        version = await repository.register_rules_version(
            checksum=CHECKSUM,
            blob_url="https://blob/rules/active/rules.json",
            now=datetime.now(UTC),
        )
        evaluation_id, count = await repository.succeed(
            run.run_id,
            evaluation=EVALUATION,
            rules_version_name=version,
            driver_key=run.driver_key,
            ended_at=datetime.now(UTC),
        )
        return run, version, evaluation_id, count


async def _one(conn, sql, **params):
    return (await conn.execute(text(sql), params)).one()


def test_success_records_every_candidate_and_completes_the_stage():
    async def run():
        async with database() as (engine, doc_id):
            attempt, version, evaluation_id, count = await _evaluate(engine, doc_id)
            assert attempt.received_date == RECEIVED
            assert version == f"sha256-{CHECKSUM[:12]}"
            assert count.counted is True
            async with engine.connect() as conn:
                row = await _one(
                    conn,
                    "SELECT rules_version, all_outcomes, selected_outcome_code, "
                    "selected_reason, priority_rank FROM rule_evaluation WHERE id = :i",
                    i=evaluation_id,
                )
                record = row[1] if isinstance(row[1], dict) else json.loads(row[1])
                assert row[0] == version
                # Every candidate, not just the winner, and the engine's own outcome.
                assert {o["action"] for o in record["candidates"]} == {"PR", "CP"}
                assert record["rule_engine_outcome_code"] == "PR"
                assert row[2] == "PR"
                assert row[3] == EVALUATION.selected_reason
                assert row[4] == EVALUATION.priority_rank
                assert await _one(
                    conn, "SELECT status, model_version FROM dmer_stage_run"
                ) == ("SUCCEEDED", version)
                assert await _one(
                    conn,
                    "SELECT pipeline_status, current_stage, attempt_count FROM dmer_document",
                ) == ("RULES_APPLIED", "DECISION", 1)
                assert await _one(
                    conn, "SELECT completed_document_count FROM driver_evaluation"
                ) == (1,)
                assert await _one(conn, "SELECT activated_by FROM rules_version") == (
                    UNREGISTERED_ACTIVATED_BY,
                )

    asyncio.run(run())


def test_replay_returns_the_committed_evaluation_without_new_rows():
    async def run():
        async with database() as (engine, doc_id):
            attempt, version, evaluation_id, _ = await _evaluate(engine, doc_id)
            async with rule_engine_session(engine, doc_id) as repository:
                with pytest.raises(RuleEngineBusyError):
                    async with rule_engine_session(engine, doc_id):
                        pytest.fail("second worker acquired the same document")
                replay = await repository.start_or_resume(now=datetime.now(UTC))
                # A late failure report cannot downgrade the committed success.
                await repository.fail(
                    attempt.run_id,
                    ended_at=datetime.now(UTC),
                    error_code="Disconnect",
                    error_detail="safe detail",
                )
            assert replay.completed.rule_evaluation_id == evaluation_id
            assert replay.completed.rules_version == version
            assert replay.completed.selected_outcome_code == "PR"
            async with engine.connect() as conn:
                assert await _one(conn, "SELECT count(*) FROM rule_evaluation") == (1,)
                assert await _one(
                    conn, "SELECT count(*), min(status::text) FROM dmer_stage_run"
                ) == (1, "SUCCEEDED")

    asyncio.run(run())


def test_a_rerun_records_a_new_evaluation_but_counts_the_document_once():
    async def run():
        async with database() as (engine, doc_id):
            await _evaluate(engine, doc_id)
            # Re-run (e.g. after a rules fix): back to NORMALIZED, evaluate again.
            async with engine.begin() as conn:
                await conn.execute(
                    text("UPDATE dmer_document SET pipeline_status = 'NORMALIZED'")
                )
            attempt, _, _, count = await _evaluate(engine, doc_id)
            assert attempt.attempt_no == 2
            assert count.counted is False and count.driver_evaluation_id is not None
            async with engine.connect() as conn:
                assert await _one(conn, "SELECT count(*) FROM rule_evaluation") == (2,)
                assert await _one(
                    conn, "SELECT completed_document_count FROM driver_evaluation"
                ) == (1,)

    asyncio.run(run())


def test_a_published_version_is_used_by_its_checksum():
    async def run():
        async with database() as (engine, doc_id):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO rules_version (version, checksum, activated_at, activated_by) "
                        "VALUES ('2026.09.1', :c, now(), 'publisher')"
                    ),
                    {"c": CHECKSUM},
                )
            _, version, _, _ = await _evaluate(engine, doc_id)
            assert version == "2026.09.1"
            async with engine.connect() as conn:
                assert await _one(conn, "SELECT count(*) FROM rules_version") == (1,)

    asyncio.run(run())


@pytest.mark.parametrize("kwargs", [{"with_driver": False}, {"open_evaluation": False}])
def test_completes_without_a_driver_evaluation_to_count(kwargs):
    async def run():
        async with database(**kwargs) as (engine, doc_id):
            _, _, _, count = await _evaluate(engine, doc_id)
            assert count.counted is False and count.driver_evaluation_id is None
            async with engine.connect() as conn:
                assert await _one(
                    conn, "SELECT pipeline_status FROM dmer_document"
                ) == ("RULES_APPLIED",)

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,error",
    [
        ("EXTRACTED", RuleEngineNotReadyError),
        ("MANUAL_REVIEW", RuleEngineStateError),
        ("RULES_APPLIED", RuleEngineStateError),  # applied, but no evaluation recorded
    ],
)
def test_documents_not_ready_or_inconsistent_are_refused(status, error):
    async def run():
        async with database(status=status) as (engine, doc_id):
            async with rule_engine_session(engine, doc_id) as repository:
                with pytest.raises(error):
                    await repository.start_or_resume(now=datetime.now(UTC))
            async with engine.connect() as conn:
                assert await _one(conn, "SELECT count(*) FROM dmer_stage_run") == (0,)

    asyncio.run(run())


def test_failed_attempt_is_recorded_and_the_next_attempt_recovers():
    async def run():
        async with database() as (engine, doc_id):
            async with rule_engine_session(engine, doc_id) as repository:
                first = await repository.start_or_resume(now=datetime.now(UTC))
                await repository.fail(
                    first.run_id,
                    ended_at=datetime.now(UTC),
                    error_code="RulesetUnavailableError",
                    error_detail="Rule evaluation attempt failed; see error_code.",
                )
            attempt, _, _, _ = await _evaluate(engine, doc_id)
            assert attempt.attempt_no == 2
            async with engine.connect() as conn:
                statuses = (
                    (
                        await conn.execute(
                            text(
                                "SELECT status::text FROM dmer_stage_run ORDER BY attempt_no"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert statuses == ["FAILED", "SUCCEEDED"]

    asyncio.run(run())


def test_an_overridden_clean_pass_keeps_the_engine_outcome_and_says_why():
    # A clean pass a driving-record row fired for is selected as IN.
    override = select_outcome(
        (
            Outcome(
                "Drugs, Alcohol and Driving",
                "CP",
                False,
                "compliant",
                driving_record_check=True,
            ),
        )
    )
    assert override.selected_outcome_code == "IN"

    async def run():
        async with database() as (engine, doc_id):
            async with rule_engine_session(engine, doc_id) as repository:
                attempt = await repository.start_or_resume(now=datetime.now(UTC))
                version = await repository.register_rules_version(
                    checksum=CHECKSUM,
                    blob_url="https://blob/rules/active/rules.json",
                    now=datetime.now(UTC),
                )
                await repository.succeed(
                    attempt.run_id,
                    evaluation=override,
                    rules_version_name=version,
                    driver_key=attempt.driver_key,
                    ended_at=datetime.now(UTC),
                )
            async with engine.connect() as conn:
                record, selected, reason = await _one(
                    conn,
                    "SELECT all_outcomes, selected_outcome_code, selected_reason FROM rule_evaluation",
                )
            record = record if isinstance(record, dict) else json.loads(record)
            assert record["rule_engine_outcome_code"] == "CP"
            assert selected == "IN"
            assert (
                reason == override.selected_reason
                and "driving record" in reason.lower()
            )

    asyncio.run(run())
