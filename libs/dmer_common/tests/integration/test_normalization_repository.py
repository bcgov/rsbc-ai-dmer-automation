"""Normalize transaction/lock behavior against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable local/CI PostgreSQL database.
Each test creates and removes its own schema; no existing tables are used.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.normalization import (
    NormalizationBusyError,
    NormalizationStateError,
    normalization_session,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_SCHEMA_SQL = (
    Path(__file__).resolve().parents[4]
    / "database/migrations/V0001__create_dmer_pipeline_schema.sql"
)


@asynccontextmanager
async def database():
    schema = "normalize_test_" + uuid4().hex
    engine = create_async_engine(
        os.environ["NORMALIZATION_TEST_DSN"],
        poolclass=NullPool,
        connect_args={"server_settings": {"search_path": schema}},
    )
    try:
        async with engine.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(_SCHEMA_SQL.read_text(encoding="utf-8"))
            document_id = str(
                (
                    await conn.execute(
                        text(
                            "INSERT INTO dmer_document (document_guid, pipeline_status, current_stage) "
                            "VALUES (gen_random_uuid(), 'EXTRACTED', 'NORMALIZE') RETURNING id"
                        )
                    )
                ).scalar_one()
            )
        yield engine, document_id
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def test_concurrent_session_is_excluded_and_committed_result_replays():
    async def run():
        async with database() as (engine, doc_id):
            async with normalization_session(engine, doc_id) as repository:
                attempt = await repository.start_or_resume(
                    now=datetime.now(UTC), model_version="model@v2"
                )
                with pytest.raises(NormalizationBusyError):
                    async with normalization_session(engine, doc_id):
                        pytest.fail("second worker acquired same document")
                await repository.succeed(
                    attempt.run_id,
                    ended_at=datetime.now(UTC),
                    output_blob_url="https://blob/normalized/first.json",
                    model_version="model@v2",
                )
                # A late failure report after an uncertain commit cannot downgrade success.
                await repository.fail(
                    attempt.run_id,
                    ended_at=datetime.now(UTC),
                    error_code="Disconnect",
                    error_detail="safe detail",
                )
            async with normalization_session(engine, doc_id) as repository:
                replay = await repository.start_or_resume(
                    now=datetime.now(UTC), model_version="new-model@v3"
                )
                assert replay.run_id == attempt.run_id
                assert replay.output_blob_url == "https://blob/normalized/first.json"
            async with engine.connect() as conn:
                assert (
                    await conn.execute(
                        text("SELECT pipeline_status, attempt_count FROM dmer_document")
                    )
                ).one() == ("NORMALIZED", 1)
                assert (
                    await conn.execute(text("SELECT status FROM dmer_stage_run"))
                ).scalar_one() == "SUCCEEDED"

    asyncio.run(run())


def test_failed_atomic_completion_rolls_back_and_next_attempt_recovers():
    async def run():
        async with database() as (engine, doc_id):
            async with normalization_session(engine, doc_id) as repository:
                first = await repository.start_or_resume(
                    now=datetime.now(UTC), model_version="model@v2"
                )
                async with engine.begin() as conn:
                    await conn.execute(
                        text(
                            "UPDATE dmer_document SET pipeline_status = 'RULES_APPLIED'"
                        )
                    )
                with pytest.raises(NormalizationStateError):
                    await repository.succeed(
                        first.run_id,
                        ended_at=datetime.now(UTC),
                        output_blob_url="https://blob/failed.json",
                        model_version="model@v2",
                    )
                async with engine.begin() as conn:
                    assert (
                        await conn.execute(text("SELECT status FROM dmer_stage_run"))
                    ).scalar_one() == "RUNNING"
                    assert (
                        await conn.execute(
                            text("SELECT pipeline_status FROM dmer_document")
                        )
                    ).scalar_one() == "RULES_APPLIED"
                    # Reproduce the older activity's partial completion before recovery.
                    await conn.execute(
                        text("UPDATE dmer_document SET pipeline_status = 'NORMALIZED'")
                    )
            async with normalization_session(engine, doc_id) as repository:
                second = await repository.start_or_resume(
                    now=datetime.now(UTC), model_version="model@v2"
                )
                assert second.attempt_no == 2
                assert second.run_id != first.run_id
                async with engine.connect() as conn:
                    rows = (
                        await conn.execute(
                            text(
                                "SELECT status, error_code FROM dmer_stage_run ORDER BY attempt_no"
                            )
                        )
                    ).all()
                    assert rows == [("FAILED", "InterruptedAttempt"), ("RUNNING", None)]

    asyncio.run(run())
