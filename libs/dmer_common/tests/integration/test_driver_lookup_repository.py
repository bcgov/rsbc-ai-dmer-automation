"""Driver Lookup unit of work against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable local/CI PostgreSQL database (the
same variable the other activity repository tests use). Each test creates and
drops its own schema with the real migrations applied.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.driver_lookup import (
    DRIVER_NOT_FOUND,
    LICENCE_LOOKUP,
    MERCURY_SUPPLIED,
    DriverLookupBusyError,
    DriverLookupNotReadyError,
    DriverLookupStateError,
    MercuryDriver,
    driver_lookup_session,
)
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
LICENCE = "01234567"
MERCURY = MercuryDriver(
    licence_number="1234567",
    mercury_driver_id="D1234",
    first_name="John",
    last_name="Doe",
)


@asynccontextmanager
async def database(*, status="EXTRACTED", supplied_driver=False, licence_read=LICENCE):
    schema = "resolve_test_" + uuid4().hex
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
                # ALTER TYPE ... ADD VALUE must commit before the value is used.
                await raw.driver_connection.execute(script.read_text(encoding="utf-8"))
        async with engine.begin() as conn:
            driver_key = None
            if supplied_driver:
                driver_key = str(
                    (
                        await conn.execute(
                            text(
                                "INSERT INTO driver (licence_number, first_name) "
                                "VALUES (:l, 'Old') RETURNING driver_key"
                            ),
                            {"l": LICENCE},
                        )
                    ).scalar_one()
                )
            document_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO dmer_document (document_guid, pipeline_status, "
                            "current_stage, driver_key) VALUES (gen_random_uuid(), "
                            "CAST(:s AS dmer_pipeline_status), 'NORMALIZE', :k) RETURNING id"
                        ),
                        {"s": status, "k": driver_key},
                    )
                ).scalar_one()
            )
            await conn.execute(
                text(
                    "INSERT INTO dmer_extraction (document_id, licence_number_read) "
                    "VALUES (:d, :l)"
                ),
                {"d": document_id, "l": licence_read},
            )
        yield engine, document_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _one(conn, sql, **params):
    return (await conn.execute(text(sql), params)).one()


async def _resolve(engine, doc_id, *, expected=2, resolved_by=LICENCE_LOOKUP):
    async with driver_lookup_session(engine, doc_id) as repository:
        run = await repository.start_or_resume(now=datetime.now(UTC))
        assert run.completed is None
        key, evaluation = await repository.attach_driver(
            run.run_id,
            mercury_driver=MERCURY,
            expected_document_count=expected,
            resolved_by=resolved_by,
            licence_mismatch=False,
            now=datetime.now(UTC),
        )
        return run, key, evaluation


def test_a_looked_up_driver_is_created_attached_and_given_an_open_evaluation():
    async def run():
        async with database() as (engine, doc_id):
            attempt, key, evaluation = await _resolve(engine, doc_id)
            assert attempt.licence_read == LICENCE and attempt.driver_key is None
            async with engine.connect() as conn:
                assert await _one(
                    conn,
                    "SELECT licence_number, mercury_driver_id, first_name FROM driver",
                ) == (LICENCE, "D1234", "John")
                assert await _one(
                    conn,
                    "SELECT driver_key::text, driver_resolved_by, licence_mismatch, "
                    "pipeline_status::text FROM dmer_document",
                ) == (key, LICENCE_LOOKUP, False, "EXTRACTED")
                assert await _one(
                    conn,
                    "SELECT id::text, status::text, open, expected_document_count, "
                    "completed_document_count FROM driver_evaluation",
                ) == (evaluation, "WAITING", True, 2, 0)
                assert await _one(
                    conn, "SELECT stage::text, status::text FROM dmer_stage_run"
                ) == ("DRIVER_LOOKUP", "SUCCEEDED")

    asyncio.run(run())


def test_a_supplied_driver_is_refreshed_and_its_licence_and_page_licence_returned():
    async def run():
        async with database(supplied_driver=True, licence_read="07654321") as (
            engine,
            doc_id,
        ):
            async with driver_lookup_session(engine, doc_id) as repository:
                attempt = await repository.start_or_resume(now=datetime.now(UTC))
                assert attempt.driver_key and attempt.driver_licence == LICENCE
                assert attempt.licence_read == "07654321"
                key, _ = await repository.attach_driver(
                    attempt.run_id,
                    mercury_driver=MERCURY,
                    expected_document_count=1,
                    resolved_by=MERCURY_SUPPLIED,
                    licence_mismatch=True,
                    now=datetime.now(UTC),
                )
            assert key == attempt.driver_key  # same driver, not a new one
            async with engine.connect() as conn:
                assert await _one(
                    conn, "SELECT count(*), max(first_name) FROM driver"
                ) == (1, "John")
                assert await _one(
                    conn,
                    "SELECT driver_resolved_by, licence_mismatch FROM dmer_document",
                ) == (MERCURY_SUPPLIED, True)

    asyncio.run(run())


def test_a_second_document_attaches_to_the_same_open_evaluation_and_sets_the_count():
    async def run():
        async with database() as (engine, first_doc):
            _, key, evaluation = await _resolve(engine, first_doc, expected=2)
            async with engine.begin() as conn:
                second_doc = str(
                    (
                        await conn.execute(
                            text(
                                "INSERT INTO dmer_document (document_guid, pipeline_status, "
                                "current_stage) VALUES (gen_random_uuid(), 'EXTRACTED', "
                                "'NORMALIZE') RETURNING id"
                            )
                        )
                    ).scalar_one()
                )
                await conn.execute(
                    text(
                        "INSERT INTO dmer_extraction (document_id, licence_number_read) "
                        "VALUES (:d, :l)"
                    ),
                    {"d": second_doc, "l": LICENCE},
                )
            _, second_key, second_evaluation = await _resolve(
                engine, second_doc, expected=3
            )
            assert (second_key, second_evaluation) == (key, evaluation)
            async with engine.connect() as conn:
                # Set from Mercury, never incremented.
                assert await _one(
                    conn,
                    "SELECT count(*), max(expected_document_count) FROM driver_evaluation",
                ) == (1, 3)

    asyncio.run(run())


def test_replay_returns_the_committed_driver_without_a_new_attempt():
    async def run():
        async with database() as (engine, doc_id):
            _, key, _ = await _resolve(engine, doc_id)
            async with driver_lookup_session(engine, doc_id) as repository:
                with pytest.raises(DriverLookupBusyError):
                    async with driver_lookup_session(engine, doc_id):
                        pytest.fail("second worker acquired the same document")
                replay = await repository.start_or_resume(now=datetime.now(UTC))
            assert replay.completed.driver_key == key
            async with engine.connect() as conn:
                assert await _one(conn, "SELECT count(*) FROM dmer_stage_run") == (1,)

    asyncio.run(run())


def test_manual_review_routes_the_document_and_replays_its_reason():
    async def run():
        async with database() as (engine, doc_id):
            async with driver_lookup_session(engine, doc_id) as repository:
                attempt = await repository.start_or_resume(now=datetime.now(UTC))
                await repository.manual_review(
                    attempt.run_id, reason=DRIVER_NOT_FOUND, now=datetime.now(UTC)
                )
            async with driver_lookup_session(engine, doc_id) as repository:
                replay = await repository.start_or_resume(now=datetime.now(UTC))
            assert replay.completed.driver_key is None
            assert replay.completed.manual_review_reason == DRIVER_NOT_FOUND
            async with engine.connect() as conn:
                assert await _one(
                    conn, "SELECT pipeline_status::text FROM dmer_document"
                ) == ("MANUAL_REVIEW",)
                assert await _one(
                    conn, "SELECT status::text, error_code FROM dmer_stage_run"
                ) == ("FAILED", DRIVER_NOT_FOUND)
                # The pipeline worked: a business route, recorded as such (I-12).
                assert await _one(
                    conn,
                    "SELECT stage::text, failure_category::text, error_class::text, "
                    "reason_code FROM processing_error",
                ) == ("DRIVER_LOOKUP", "PERMANENT_BUSINESS", "POISON", DRIVER_NOT_FOUND)
                assert await _one(conn, "SELECT count(*) FROM driver_evaluation") == (
                    0,
                )

    asyncio.run(run())


def test_failed_attempt_is_recorded_and_the_next_attempt_recovers():
    async def run():
        async with database() as (engine, doc_id):
            async with driver_lookup_session(engine, doc_id) as repository:
                first = await repository.start_or_resume(now=datetime.now(UTC))
                await repository.fail(
                    first.run_id,
                    ended_at=datetime.now(UTC),
                    error_code="MercuryApiError",
                    error_detail="Driver resolution attempt failed; see error_code.",
                )
            attempt, _, _ = await _resolve(engine, doc_id)
            assert attempt.attempt_no == 2

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,error",
    [
        ("EXTRACTING", DriverLookupNotReadyError),
        ("MANUAL_REVIEW", DriverLookupStateError),
    ],
)
def test_documents_not_ready_or_reviewed_elsewhere_are_refused(status, error):
    async def run():
        async with (
            database(status=status) as (engine, doc_id),
            driver_lookup_session(engine, doc_id) as repository,
        ):
            with pytest.raises(error):
                await repository.start_or_resume(now=datetime.now(UTC))

    asyncio.run(run())
