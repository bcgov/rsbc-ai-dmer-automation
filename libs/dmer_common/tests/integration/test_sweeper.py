"""The Reconciliation Sweeper's queries against an isolated PostgreSQL schema
(09-reliability-components.md "Reconciliation Sweeper").

Set NORMALIZATION_TEST_DSN to a disposable PostgreSQL database.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.mercury_outbox import fallback_rows, queue_outbox_rows
from dmer_common.db.sweeper import (
    SweeperSettings,
    SweepQueue,
    outbox_backlog,
    sweep_stale_evaluations,
    sweep_stalled_documents,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
OLD = NOW - timedelta(hours=1)  # past the 30-minute SLA
SETTINGS = SweeperSettings(max_republish=2)


@asynccontextmanager
async def database():
    schema = "sweeper_test_" + uuid4().hex
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


async def _driver(engine):
    async with engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO driver (licence_number) VALUES (:l) "
                        "RETURNING driver_key"
                    ),
                    {"l": uuid4().hex[:8]},
                )
            ).scalar_one()
        )


async def _document(
    engine, status, *, updated_at=OLD, driver_key=None, raw_blob_url=None
):
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO dmer_document (document_guid, pipeline_status, "
                    "current_stage, driver_key, raw_blob_url, updated_at) VALUES "
                    "(gen_random_uuid(), :s, 'EXTRACT', :k, :r, :u) RETURNING id::text"
                ),
                {"s": status, "k": driver_key, "r": raw_blob_url, "u": updated_at},
            )
        ).scalar_one()


async def _stage_output(engine, document_id, stage, url):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dmer_stage_run (document_id, stage, status, attempt_no, "
                "started_at, ended_at, output_blob_url) VALUES (:d, :s, "
                "'SUCCEEDED', 1, now(), now(), :u)"
            ),
            {"d": document_id, "s": stage, "u": url},
        )


async def _evaluation(engine, driver_key, status="WAITING", checked_at=OLD):
    async with engine.begin() as conn:
        return str(
            (
                await conn.execute(
                    text(
                        "INSERT INTO driver_evaluation (driver_key, status, open, "
                        "completed_document_count, last_mercury_check_at) VALUES "
                        "(:k, :s, true, 0, :c) RETURNING id"
                    ),
                    {"k": driver_key, "s": status, "c": checked_at},
                )
            ).scalar_one()
        )


async def _one(engine, sql, **params):
    async with engine.connect() as conn:
        return (await conn.execute(text(sql), params)).one()


async def _errors(engine, document_id):
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text(
                    "SELECT stage::text, failure_category::text, reason_code, "
                    "resolution, redrive_count FROM processing_error "
                    "WHERE document_id = :d ORDER BY id"
                ),
                {"d": document_id},
            )
        ).all()


def _sweep(engine, now=NOW):
    return sweep_stalled_documents(engine, settings=SETTINGS, now=now)


def test_a_stalled_document_is_republished_to_the_queue_its_status_waits_on():
    async def run():
        async with database() as engine:
            received = await _document(engine, "RECEIVED")
            extracting = await _document(engine, "EXTRACTING", raw_blob_url="raw.pdf")
            extracted = await _document(engine, "EXTRACTED", raw_blob_url="raw.pdf")
            await _stage_output(engine, extracted, "EXTRACT", "combined.json")
            result = await _sweep(engine)
            sent = {
                r.document_id: (r.queue, r.blob_url, r.sweep) for r in result.republish
            }
            assert sent == {
                received: (SweepQueue.INGEST, None, 1),
                extracting: (SweepQueue.RAW, "raw.pdf", 1),
                extracted: (SweepQueue.EXTRACTED, "combined.json", 1),
            }
            status, attempts, updated = await _one(
                engine,
                "SELECT pipeline_status::text, attempt_count, updated_at "
                "FROM dmer_document WHERE id = :d",
                d=extracting,
            )
            assert (status, attempts, updated) == ("EXTRACTING", 1, NOW)
            assert await _errors(engine, extracting) == [
                ("EXTRACT", "TRANSIENT", "TRANSIENT_STALLED", "REPUBLISHED", 1)
            ]

    asyncio.run(run())


def test_a_missing_artifact_falls_back_to_the_stage_before():
    async def run():
        async with database() as engine:
            # EXTRACTED with no extraction output and no raw blob: start over
            doc = await _document(engine, "EXTRACTED")
            ((item,),) = [(await _sweep(engine)).republish]
            assert (item.document_id, item.queue) == (doc, SweepQueue.INGEST)

    asyncio.run(run())


def test_documents_within_their_sla_or_not_in_flight_are_left_alone():
    async def run():
        async with database() as engine:
            await _document(engine, "RECEIVED", updated_at=NOW - timedelta(minutes=5))
            for status in ("MANUAL_REVIEW", "COMPLETED", "POSTING", "RULES_APPLIED"):
                await _document(engine, status)
            result = await _sweep(engine)
            assert result.republish == [] and result.manual_review == []

    asyncio.run(run())


def test_a_document_that_keeps_stalling_goes_to_a_person():
    async def run():
        async with database() as engine:
            doc = await _document(engine, "RECEIVED")
            now = NOW
            for sweep in (1, 2):
                result = await _sweep(engine, now)
                assert [r.sweep for r in result.republish] == [sweep]
                now += timedelta(hours=1)
            result = await _sweep(engine, now)
            assert result.republish == [] and result.manual_review == [doc]
            (status,) = await _one(
                engine,
                "SELECT pipeline_status::text FROM dmer_document WHERE id = :d",
                d=doc,
            )
            assert status == "MANUAL_REVIEW"
            assert (await _errors(engine, doc))[-1][1:4] == (
                "UNKNOWN",
                "UNKNOWN_STALLED",
                "MANUAL_REVIEW",
            )
            # no decision, no Mercury write: a stall is not a business failure
            (outbox,) = await _one(engine, "SELECT count(*) FROM mercury_outbox")
            assert outbox == 0

    asyncio.run(run())


def test_a_stuck_waiting_evaluation_is_marked_stale_and_re_signalled():
    async def run():
        async with database() as engine:
            driver = await _driver(engine)
            evaluation = await _evaluation(engine, driver)
            first = await _document(
                engine, "AWAITING_DRIVER_COMPLETION", driver_key=driver
            )
            last = await _document(
                engine,
                "RULES_APPLIED",
                driver_key=driver,
                updated_at=OLD + timedelta(minutes=1),
            )
            await _stage_output(engine, last, "NORMALIZE", "normalized.json")
            (signal,) = await sweep_stale_evaluations(
                engine, settings=SETTINGS, now=NOW
            )
            assert signal.queue is SweepQueue.DRIVER_DECISION
            assert (signal.driver_key, signal.document_id) == (driver, last)
            assert signal.blob_url == "normalized.json"
            (status,) = await _one(
                engine,
                "SELECT status::text FROM driver_evaluation WHERE id = :e",
                e=evaluation,
            )
            assert status == "STALE"
            assert await _errors(engine, first) == []
            assert (await _errors(engine, last))[0][:3] == (
                "DECISION",
                "TRANSIENT",
                "TRANSIENT_STALLED",
            )

    asyncio.run(run())


def test_an_evaluation_with_documents_in_flight_or_recent_activity_is_not_signalled():
    async def run():
        async with database() as engine:
            in_flight = await _driver(engine)
            await _evaluation(engine, in_flight)
            await _document(engine, "RULES_APPLIED", driver_key=in_flight)
            await _document(engine, "EXTRACTED", driver_key=in_flight)  # will signal
            recent = await _driver(engine)
            await _evaluation(engine, recent, checked_at=NOW - timedelta(minutes=5))
            await _document(engine, "RULES_APPLIED", driver_key=recent)
            decided = await _driver(engine)
            await _evaluation(engine, decided, status="DECIDED")
            await _document(engine, "RULES_APPLIED", driver_key=decided)
            assert (
                await sweep_stale_evaluations(engine, settings=SETTINGS, now=NOW) == []
            )

    asyncio.run(run())


def test_the_outbox_backlog_counts_what_needs_an_alert():
    async def run():
        async with database() as engine:
            docs = [await _document(engine, "MANUAL_REVIEW") for _ in range(3)]
            async with engine.begin() as conn:
                for doc in docs:
                    await queue_outbox_rows(
                        conn,
                        fallback_rows(
                            source_id=f"{doc}:FALLBACK",
                            document_id=doc,
                            document_guid=str(uuid4()),
                            comment="AI could not process.",
                        ),
                        now=OLD - timedelta(hours=1),
                    )
                await conn.execute(
                    text(
                        "UPDATE mercury_outbox SET attempt_count = 7 "
                        "WHERE document_id = :d"
                    ),
                    {"d": docs[1]},
                )
                await conn.execute(
                    text(
                        "UPDATE mercury_outbox SET status = 'FAILED' WHERE document_id = :d"
                    ),
                    {"d": docs[2]},
                )
            backlog = await outbox_backlog(engine, settings=SETTINGS, now=NOW)
            assert (
                backlog.pending_past_sla,
                backlog.high_attempts,
                backlog.failed,
            ) == (
                2,
                1,
                1,
            )
            assert backlog.alert

    asyncio.run(run())
