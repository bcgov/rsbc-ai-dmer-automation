"""The daily reconciliation report's checks against an isolated PostgreSQL
schema (09-reliability-components.md "Daily reconciliation report").

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
from dmer_common.db.reconciliation import (
    Finding,
    MercuryDocument,
    daily_metrics,
    reconcile,
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
DAY_AGO = NOW - timedelta(days=2)


@asynccontextmanager
async def database():
    schema = "recon_test_" + uuid4().hex
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


async def _document(engine, status, *, first_seen=DAY_AGO, updated=NOW):
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO dmer_document (document_guid, pipeline_status, "
                    "current_stage, first_seen_at, updated_at) VALUES "
                    "(gen_random_uuid(), :s, 'POST', :f, :u) "
                    "RETURNING id::text, document_guid::text"
                ),
                {"s": status, "f": first_seen, "u": updated},
            )
        ).one()
    return tuple(row)


async def _fallback(engine, document_id, guid, status="PENDING"):
    async with engine.begin() as conn:
        await queue_outbox_rows(
            conn,
            fallback_rows(
                source_id=f"{document_id}:FALLBACK",
                document_id=document_id,
                document_guid=guid,
                comment="AI could not process.",
            ),
            now=NOW,
        )
        await conn.execute(
            text("UPDATE mercury_outbox SET status = :s WHERE document_id = :d"),
            {"s": status, "d": document_id},
        )


def _reconcile(engine, documents):
    return reconcile(
        engine,
        documents,
        now=NOW,
        receive_grace=timedelta(hours=2),
        overdue=timedelta(hours=24),
    )


def test_every_unaccounted_document_is_reported_with_why():
    async def run():
        async with database() as engine:
            in_progress = await _document(engine, "EXTRACTED")
            fresh = await _document(engine, "EXTRACTED", first_seen=NOW)
            manual = await _document(engine, "MANUAL_REVIEW")
            manual_with_fallback = await _document(engine, "MANUAL_REVIEW")
            await _fallback(engine, *manual_with_fallback, status="SENT")
            pending = await _document(engine, "POSTING")
            await _fallback(engine, *pending, status="PENDING")
            failed = await _document(engine, "POSTING")
            await _fallback(engine, *failed, status="FAILED")
            done = await _document(engine, "COMPLETED")
            await _fallback(engine, *done, status="SENT")
            missing, just_arrived = str(uuid4()), str(uuid4())
            documents = [
                MercuryDocument(guid, DAY_AGO)
                for _, guid in (
                    in_progress,
                    fresh,
                    manual,
                    manual_with_fallback,
                    pending,
                    failed,
                    done,
                )
            ] + [
                MercuryDocument(missing, DAY_AGO),
                MercuryDocument(just_arrived, NOW - timedelta(minutes=30)),
            ]
            entries = await _reconcile(engine, documents)
            found = {e.document_guid: e.finding for e in entries}
            assert found == {
                in_progress[1]: Finding.OVERDUE,
                manual[1]: Finding.MANUAL_REVIEW_NO_OUTCOME,
                pending[1]: Finding.NOT_POSTED,
                failed[1]: Finding.POST_FAILED,
                missing: Finding.NOT_RECEIVED,
            }
            (missing_entry,) = [e for e in entries if e.document_guid == missing]
            assert missing_entry.document_id is None
            (manual_entry,) = [e for e in entries if e.document_guid == manual[1]]
            assert manual_entry.as_dict()["pipeline_status"] == "MANUAL_REVIEW"

    asyncio.run(run())


def test_daily_metrics_count_the_last_24_hours():
    async def run():
        async with database() as engine:
            today = await _document(engine, "MANUAL_REVIEW")
            await _fallback(engine, *today)
            await _document(engine, "MANUAL_REVIEW", updated=NOW - timedelta(days=3))
            await _document(engine, "COMPLETED")
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO dmer_stage_run (document_id, stage, status, "
                        "attempt_no, started_at, ended_at) VALUES "
                        "(:d, 'EXTRACT', 'SUCCEEDED', 1, :s, :e)"
                    ),
                    {"d": today[0], "s": NOW - timedelta(seconds=90), "e": NOW},
                )
            metrics = await daily_metrics(engine, now=NOW)
            assert metrics["manual_review_24h"] == 1
            assert metrics["fallbacks_24h"] == 1
            assert metrics["stage_p95_seconds"] == {"EXTRACT": 90.0}
            assert metrics["stage_runs_24h"] == {"EXTRACT": 1}

    asyncio.run(run())
