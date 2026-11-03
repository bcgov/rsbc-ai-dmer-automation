"""The DLQ Drain's database side against an isolated PostgreSQL schema
(stages/10-dlq-drain.md "Required behaviour-spec tests").

Set NORMALIZATION_TEST_DSN to a disposable PostgreSQL database.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.dlq_drain import DeadLetter, DrainAction, drain_dead_letter
from dmer_common.db.mercury_outbox import fallback_rows, queue_outbox_rows
from dmer_common.db.status import PipelineStatus
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
T0 = datetime(2026, 10, 19, 12, tzinfo=UTC)


@asynccontextmanager
async def database():
    schema = "dlq_test_" + uuid4().hex
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


async def _document(engine, status="MANUAL_REVIEW", stage="EXTRACT"):
    async with engine.begin() as conn:
        return (
            await conn.execute(
                text(
                    "INSERT INTO dmer_document (document_guid, pipeline_status, "
                    "current_stage) VALUES (gen_random_uuid(), :s, :st) "
                    "RETURNING id::text"
                ),
                {"s": status, "st": stage},
            )
        ).scalar_one()


async def _stage_error(engine, document_id, stage, error_code):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO dmer_stage_run (document_id, stage, status, attempt_no, "
                "started_at, ended_at, error_code) VALUES (:d, :s, 'FAILED', 1, "
                "now(), now(), :e)"
            ),
            {"d": document_id, "s": stage, "e": error_code},
        )


async def _state(engine, document_id):
    async with engine.connect() as conn:
        status, attempts = (
            await conn.execute(
                text(
                    "SELECT pipeline_status::text, attempt_count FROM dmer_document "
                    "WHERE id = :d"
                ),
                {"d": document_id},
            )
        ).one()
        errors = (
            await conn.execute(
                text(
                    "SELECT failure_category::text, reason_code, resolution, "
                    "redrive_count, dlq_message_id FROM processing_error "
                    "WHERE document_id = :d ORDER BY id"
                ),
                {"d": document_id},
            )
        ).all()
        outbox = (
            await conn.execute(
                text(
                    "SELECT operation::text, payload, idempotency_key FROM "
                    "mercury_outbox WHERE document_id = :d"
                ),
                {"d": document_id},
            )
        ).all()
        decisions = (
            await conn.execute(
                text("SELECT count(*) FROM dmer_decision WHERE document_id = :d"),
                {"d": document_id},
            )
        ).scalar_one()
    return status, attempts, errors, outbox, decisions


def _raw(document_id, reason, description="", message_id="m-1"):
    """A dead letter from dmer-raw (extraction)."""
    return DeadLetter(
        queue="dmer-raw",
        stage="EXTRACT",
        message_id=message_id,
        document_id=document_id,
        reason=reason,
        description=description,
        entry_status=PipelineStatus.DOWNLOADED,
    )


def _drain(engine, dead_letter, max_redrives=2):
    return drain_dead_letter(engine, dead_letter, max_redrives=max_redrives, now=T0)


def test_permanent_failure_routes_to_manual_review_with_the_fallback_in():
    async def run():
        async with database() as engine:
            doc = await _document(engine)
            result = await _drain(
                engine, _raw(doc, "PDF_UNREADABLE", "error=PdfReadError")
            )
            status, _, errors, outbox, decisions = await _state(engine, doc)
            assert result.action is DrainAction.FALLBACK_QUEUED
            assert status == "MANUAL_REVIEW"
            assert [(e[0], e[1], e[2]) for e in errors] == [
                (
                    "PERMANENT_BUSINESS",
                    "PERMANENT_UNREADABLE_DOCUMENT",
                    "FALLBACK_QUEUED",
                )
            ]
            ((operation, payload, key),) = outbox
            assert operation == "UPDATE_OUTCOME"
            assert payload["outcome_code"] == "IN"
            assert payload["decided_by"] == "FALLBACK"
            assert payload["comment"].startswith("AI could not process.")
            assert "could not be read" in payload["comment"]
            assert key == f"{doc}:FALLBACK:UPDATE_OUTCOME"
            assert decisions == 0  # no driver evaluation: the outbox row alone

    asyncio.run(run())


def test_transient_failure_is_redriven_with_the_document_reopened():
    async def run():
        async with database() as engine:
            doc = await _document(engine)  # the DI processor set MANUAL_REVIEW
            result = await _drain(engine, _raw(doc, "DB_WRITE_FAILED"))
            status, attempts, errors, outbox, decisions = await _state(engine, doc)
            assert (result.action, result.redrive_attempt) == (DrainAction.REDRIVEN, 1)
            assert status == "DOWNLOADED"  # dmer-raw's entry status
            assert attempts == 1
            assert [(e[0], e[2], e[3]) for e in errors] == [
                ("TRANSIENT", "REDRIVEN", 1)
            ]
            assert outbox == [] and decisions == 0

    asyncio.run(run())


def test_dependency_failure_is_redriven_and_writes_no_decision():
    async def run():
        async with database() as engine:
            doc = await _document(engine)
            result = await _drain(
                engine,
                _raw(
                    doc, "LLM_CALL_FAILED", "error=HttpResponseError; http_status=429"
                ),
            )
            status, _, errors, outbox, decisions = await _state(engine, doc)
            assert result.action is DrainAction.REDRIVEN
            assert status == "DOWNLOADED"
            assert errors[0][:2] == ("PROCESSING", "PROCESSING_DEPENDENCY_THROTTLED")
            assert outbox == [] and decisions == 0

    asyncio.run(run())


def test_redrives_are_bounded_then_the_document_goes_to_a_person():
    async def run():
        async with database() as engine:
            doc = await _document(engine)
            for n in (1, 2):
                result = await _drain(
                    engine, _raw(doc, "DB_READ_FAILED", message_id=f"m-{n}")
                )
                assert (result.action, result.redrive_attempt) == (
                    DrainAction.REDRIVEN,
                    n,
                )
                async with engine.begin() as conn:  # it fails again
                    await conn.execute(
                        text(
                            "UPDATE dmer_document SET pipeline_status = 'MANUAL_REVIEW'"
                        )
                    )
            result = await _drain(engine, _raw(doc, "DB_READ_FAILED", message_id="m-3"))
            status, _, errors, outbox, decisions = await _state(engine, doc)
            assert result.action is DrainAction.MANUAL_REVIEW
            assert status == "MANUAL_REVIEW"
            assert errors[-1][:3] == (
                "UNKNOWN",
                "UNKNOWN_REDRIVE_EXHAUSTED",
                "MANUAL_REVIEW",
            )
            assert outbox == [] and decisions == 0

    asyncio.run(run())


def test_logical_failure_goes_to_manual_review_without_a_decision():
    async def run():
        async with database() as engine:
            doc = await _document(engine, status="EXTRACTED", stage="NORMALIZE")
            await _stage_error(engine, doc, "NORMALIZE", "InvalidStatusTransition")
            dead_letter = DeadLetter(
                queue="dmer-extracted",
                stage="NORMALIZE",
                message_id="m-1",
                document_id=doc,
                reason="MaxDeliveryCountExceeded",
                description="Message could not be consumed after 10 delivery attempts.",
                entry_status=PipelineStatus.EXTRACTED,
            )
            result = await _drain(engine, dead_letter)
            status, _, errors, outbox, decisions = await _state(engine, doc)
            assert result.action is DrainAction.MANUAL_REVIEW
            assert status == "MANUAL_REVIEW"
            assert errors[0][:2] == ("UNKNOWN", "UNKNOWN_LOGICAL_ERROR")
            assert outbox == [] and decisions == 0

    asyncio.run(run())


def test_draining_the_same_message_twice_records_it_once():
    async def run():
        async with database() as engine:
            doc = await _document(engine)
            first = await _drain(engine, _raw(doc, "PDF_UNREADABLE"))
            again = await _drain(engine, _raw(doc, "PDF_UNREADABLE"))
            _, _, errors, outbox, _ = await _state(engine, doc)
            assert first.action is again.action is DrainAction.FALLBACK_QUEUED
            assert again.already_drained
            assert len(errors) == 1 and len(outbox) == 1

    asyncio.run(run())


def test_a_document_with_a_fallback_already_queued_gets_no_second_one():
    async def run():
        async with database() as engine:
            doc = await _document(engine)
            async with engine.begin() as conn:  # e.g. Driver Lookup's I-22 fallback
                await queue_outbox_rows(
                    conn,
                    fallback_rows(
                        source_id=f"{doc}:FALLBACK",
                        document_id=doc,
                        document_guid=str(uuid4()),
                        comment="AI could not process. Earlier reason.",
                    ),
                    now=T0,
                )
            result = await _drain(engine, _raw(doc, "PDF_UNREADABLE", message_id="m-2"))
            _, _, errors, outbox, _ = await _state(engine, doc)
            assert result.action is DrainAction.MANUAL_REVIEW
            assert len(outbox) == 1
            assert outbox[0][1]["comment"] == "AI could not process. Earlier reason."
            assert errors[0][1] == "PERMANENT_UNREADABLE_DOCUMENT"

    asyncio.run(run())


def test_a_document_with_a_decision_is_never_reopened():
    async def run():
        async with database() as engine:
            doc = await _document(engine, status="POSTING", stage="POST")
            result = await _drain(engine, _raw(doc, "DB_WRITE_FAILED"))
            status, _, errors, outbox, _ = await _state(engine, doc)
            assert result.action is DrainAction.STALE
            assert status == "POSTING"
            assert errors[0][2] == "STALE" and outbox == []

    asyncio.run(run())


def test_a_driver_level_dead_letter_is_recorded_without_touching_the_document():
    async def run():
        async with database() as engine:
            doc = await _document(
                engine, status="AWAITING_DRIVER_COMPLETION", stage="DECISION"
            )
            await _stage_error(engine, doc, "DECISION", "DriverDecisionStateError")
            dead_letter = DeadLetter(
                queue="driver-decision",
                stage="DECISION",
                message_id="m-1",
                document_id=doc,
                reason="MaxDeliveryCountExceeded",
                description="",
                entry_status=None,
                routes_document=False,
            )
            result = await _drain(engine, dead_letter)
            status, _, errors, _, _ = await _state(engine, doc)
            assert result.action is DrainAction.RECORDED
            assert status == "AWAITING_DRIVER_COMPLETION"
            assert errors[0][:2] == ("UNKNOWN", "UNKNOWN_LOGICAL_ERROR")

    asyncio.run(run())


def test_a_dead_letter_without_a_known_document_is_not_recorded():
    async def run():
        async with database() as engine:
            result = await _drain(engine, _raw(str(uuid4()), "PDF_UNREADABLE"))
            assert result.action is DrainAction.NO_DOCUMENT

    asyncio.run(run())
