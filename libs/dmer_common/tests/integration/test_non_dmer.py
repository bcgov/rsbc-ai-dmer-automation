"""Non-DMER records: stored as MANUAL_REVIEW with an error outcome queued for
Mercury. Set NORMALIZATION_TEST_DSN to a disposable PostgreSQL database."""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.non_dmer import not_a_dmer_comment, record_non_dmer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
NOW = datetime(2026, 11, 23, 12, tzinfo=UTC)
HRDIR = {
    "document_guid": "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f",
    "document_type": "HRDIR High Risk Driving Incident Report",
    "document_name": "report.pdf",
    "document_status": "Uploaded",
    "queue": "Team - Intake",
    "case": {"case_id": "C1"},
}


@asynccontextmanager
async def database():
    schema = "non_dmer_test_" + uuid4().hex
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
        yield engine
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


async def _rows(engine):
    async with engine.connect() as conn:
        docs = (
            await conn.execute(
                text(
                    "SELECT id::text, pipeline_status::text, current_stage::text, "
                    "driver_key, mercury_case_id FROM dmer_document"
                )
            )
        ).all()
        errors = (
            await conn.execute(
                text(
                    "SELECT stage::text, failure_category, reason_code "
                    "FROM processing_error"
                )
            )
        ).all()
        outbox = (
            await conn.execute(
                text(
                    "SELECT operation::text, payload, idempotency_key, "
                    "status::text, next_attempt_at FROM mercury_outbox"
                )
            )
        ).all()
    return docs, errors, outbox


def test_a_non_dmer_is_stored_for_review_with_an_error_outcome_queued():
    async def run():
        async with database() as engine:
            result = await record_non_dmer(
                engine, HRDIR, now=NOW, send_after=timedelta(minutes=5)
            )
            assert result.queued is True
            docs, errors, outbox = await _rows(engine)
            ((doc_id, status, stage, driver_key, case_id),) = docs
            assert (status, stage, driver_key, case_id) == (
                "MANUAL_REVIEW",
                "INGEST",
                None,
                "C1",
            )
            assert errors == [("INGEST", "PERMANENT_BUSINESS", "NOT_A_DMER")]
            ((operation, payload, key, row_status, due),) = outbox
            assert (operation, row_status) == ("UPDATE_OUTCOME", "PENDING")
            assert key == f"{doc_id}:FALLBACK:UPDATE_OUTCOME"
            assert payload == {
                "document_guid": HRDIR["document_guid"],
                "comment": not_a_dmer_comment(HRDIR["document_type"]),
                "outcome_code": "IN",
                "decided_by": "FALLBACK",  # sent as aiProcessedStatus Error
            }
            assert due == NOW + timedelta(minutes=5)  # after the claim

    asyncio.run(run())


def test_a_re_polled_non_dmer_is_not_queued_twice():
    async def run():
        async with database() as engine:
            await record_non_dmer(engine, HRDIR, now=NOW)
            again = await record_non_dmer(engine, HRDIR, now=NOW)
            assert again.queued is False
            docs, errors, outbox = await _rows(engine)
            assert (len(docs), len(errors), len(outbox)) == (1, 1, 1)

    asyncio.run(run())


def test_a_guid_already_in_the_pipeline_as_a_dmer_is_left_alone():
    async def run():
        async with database() as engine:
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO dmer_document (document_guid, pipeline_status, "
                        "current_stage) VALUES (:g, 'RECEIVED', 'INGEST')"
                    ),
                    {"g": HRDIR["document_guid"]},
                )
            result = await record_non_dmer(engine, HRDIR, now=NOW)
            assert result.queued is False
            docs, errors, outbox = await _rows(engine)
            assert docs[0][1] == "RECEIVED"
            assert (errors, outbox) == ([], [])

    asyncio.run(run())


def test_the_note_says_it_is_not_a_dmer():
    assert not_a_dmer_comment("HRDIR High Risk Driving Incident Report") == (
        "Unable to AI process due to error. The document is not a DMER "
        "(document type: HRDIR High Risk Driving Incident Report)."
    )
    assert not_a_dmer_comment(None).endswith("(document type: unknown).")
