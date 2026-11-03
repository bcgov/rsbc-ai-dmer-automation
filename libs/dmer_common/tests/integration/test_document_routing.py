"""Document Orchestration status writes against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable local/CI PostgreSQL database.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.document_routing import (
    load_signal_state,
    mark_awaiting_driver_completion,
    route_to_manual_review,
)
from dmer_common.db.processing_error import FailureCategory
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


@asynccontextmanager
async def document(status: str, *, with_driver: bool = True):
    schema = "routing_test_" + uuid4().hex
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
                driver_key = (
                    await conn.execute(
                        text(
                            "INSERT INTO driver (licence_number) VALUES ('01234567') RETURNING driver_key"
                        )
                    )
                ).scalar_one()
            doc_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO dmer_document (document_guid, pipeline_status, current_stage, driver_key) "
                            "VALUES (gen_random_uuid(), CAST(:s AS dmer_pipeline_status), 'DECISION', :k) RETURNING id"
                        ),
                        {"s": status, "k": driver_key},
                    )
                ).scalar_one()
            )
        yield engine, doc_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _status(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT d.pipeline_status::text, e.reason_code FROM dmer_document d "
                    "LEFT JOIN processing_error e ON e.document_id = d.id"
                )
            )
        ).one()


def test_signal_state_and_the_move_to_awaiting_driver_completion():
    async def run():
        async with document("RULES_APPLIED") as (engine, doc_id):
            state = await load_signal_state(engine, doc_id)
            assert state.pipeline_status == "RULES_APPLIED" and state.driver_key
            assert await mark_awaiting_driver_completion(
                engine, doc_id, now=datetime.now(UTC)
            )
            assert (
                await mark_awaiting_driver_completion(
                    engine, doc_id, now=datetime.now(UTC)
                )
                is False
            )
            assert await _status(engine) == ("AWAITING_DRIVER_COMPLETION", None)
            assert await load_signal_state(engine, str(uuid4())) is None

    asyncio.run(run())


def test_only_a_rules_applied_document_moves_to_awaiting():
    async def run():
        async with document("NORMALIZED") as (engine, doc_id):
            assert (
                await mark_awaiting_driver_completion(
                    engine, doc_id, now=datetime.now(UTC)
                )
                is False
            )
            assert await _status(engine) == ("NORMALIZED", None)

    asyncio.run(run())


@pytest.mark.parametrize(
    "status", ["EXTRACTED", "NORMALIZED", "RULES_APPLIED", "AWAITING_DRIVER_COMPLETION"]
)
def test_a_non_terminal_document_is_routed_with_its_reason(status):
    async def run():
        async with document(status) as (engine, doc_id):
            assert await route_to_manual_review(
                engine,
                doc_id,
                stage="NORMALIZE",
                category=FailureCategory.UNKNOWN,
                reason_code="NormalizeDmer:RETRIES_EXHAUSTED",
                message="fixed description",
                now=datetime.now(UTC),
            )
            assert await _status(engine) == (
                "MANUAL_REVIEW",
                "NormalizeDmer:RETRIES_EXHAUSTED",
            )
            async with engine.connect() as conn:
                row = (
                    await conn.execute(
                        text(
                            "SELECT stage::text, failure_category::text, error_class::text, "
                            "message, redrive_count FROM processing_error"
                        )
                    )
                ).one()
            # UNKNOWN has no legacy error_class.
            assert tuple(row) == ("NORMALIZE", "UNKNOWN", None, "fixed description", 0)

    asyncio.run(run())


@pytest.mark.parametrize("status", ["MANUAL_REVIEW", "COMPLETED"])
def test_a_terminal_document_is_left_alone(status):
    async def run():
        async with document(status) as (engine, doc_id):
            assert (
                await route_to_manual_review(
                    engine,
                    doc_id,
                    stage="RULES",
                    category=FailureCategory.UNKNOWN,
                    reason_code="X:Y",
                    message="m",
                    now=datetime.now(UTC),
                )
                is False
            )
            assert await _status(engine) == (status, None)  # no processing_error row

    asyncio.run(run())


@pytest.mark.parametrize(
    "category,legacy",
    [
        (FailureCategory.PERMANENT_BUSINESS, "POISON"),
        (FailureCategory.TRANSIENT, "TRANSIENT"),
        (FailureCategory.PROCESSING, "DOWNSTREAM"),
    ],
)
def test_each_category_also_sets_its_legacy_error_class(category, legacy):
    async def run():
        async with document("RULES_APPLIED") as (engine, doc_id):
            await route_to_manual_review(
                engine,
                doc_id,
                stage="RULES",
                category=category,
                reason_code="RunRuleEngine:X",
                message="m",
                now=datetime.now(UTC),
            )
            async with engine.connect() as conn:
                assert (
                    await conn.execute(
                        text("SELECT error_class::text FROM processing_error")
                    )
                ).scalar_one() == legacy

    asyncio.run(run())


# --- counting a failed document into its driver's batch ----------------------


async def _route(engine, doc_id):
    return await route_to_manual_review(
        engine,
        doc_id,
        stage="NORMALIZE",
        category=FailureCategory.PERMANENT_BUSINESS,
        reason_code="NormalizeDmer:NormalizationValidationError",
        message="fixed description",
        now=datetime.now(UTC),
    )


async def _open_evaluation(engine):
    """A WAITING open evaluation for the document's driver; returns its id."""
    async with engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO driver_evaluation (driver_key, expected_document_count) "
                        "SELECT driver_key, 2 FROM driver RETURNING id"
                    )
                )
            ).scalar_one()
        )


async def _batch(engine):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT e.id::text, e.open, e.completed_document_count, "
                    "count(m.document_id) FROM driver_evaluation e "
                    "LEFT JOIN driver_evaluation_document m "
                    "ON m.driver_evaluation_id = e.id GROUP BY e.id ORDER BY e.open"
                )
            )
        ).all()


def test_a_failed_document_with_a_driver_is_counted_into_its_open_batch():
    # So the Decision Gateway decides the rest of the batch instead of waiting
    # for a document that will never reach the Rule Engine.
    async def run():
        async with document("NORMALIZED") as (engine, doc_id):
            evaluation_id = await _open_evaluation(engine)
            assert await _route(engine, doc_id)
            assert await _batch(engine) == [(evaluation_id, True, 1, 1)]

    asyncio.run(run())


def test_without_an_open_batch_one_is_opened_for_the_failed_document():
    async def run():
        async with document("NORMALIZED") as (engine, doc_id):
            assert await _route(engine, doc_id)
            ((_, is_open, completed, markers),) = await _batch(engine)
            assert (is_open, completed, markers) == (True, 1, 1)

    asyncio.run(run())


def test_a_document_already_counted_by_the_rule_engine_is_not_counted_twice():
    # e.g. it failed in Signal Driver, after the Rule Engine counted it.
    async def run():
        async with document("RULES_APPLIED") as (engine, doc_id):
            evaluation_id = await _open_evaluation(engine)
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO driver_evaluation_document "
                        "(driver_evaluation_id, document_id, counted_at) "
                        "VALUES (:e, :d, now())"
                    ),
                    {"e": evaluation_id, "d": doc_id},
                )
                await conn.execute(
                    text("UPDATE driver_evaluation SET completed_document_count = 1")
                )
            assert await _route(engine, doc_id)
            assert await _batch(engine) == [(evaluation_id, True, 1, 1)]

    asyncio.run(run())


def test_a_failed_document_without_a_driver_is_not_counted():
    async def run():
        async with document("EXTRACTED", with_driver=False) as (engine, doc_id):
            assert await _route(engine, doc_id)
            assert await _batch(engine) == []

    asyncio.run(run())


def test_a_terminal_document_is_not_counted():
    async def run():
        async with document("COMPLETED") as (engine, doc_id):
            evaluation_id = await _open_evaluation(engine)
            assert not await _route(engine, doc_id)
            assert await _batch(engine) == [(evaluation_id, True, 0, 0)]

    asyncio.run(run())
