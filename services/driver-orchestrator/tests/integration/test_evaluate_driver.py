"""evaluate_driver end to end against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable PostgreSQL database (as for the
dmer_common repository tests). Mercury is faked; everything else is real:
the migrations, the advisory lock, the queries and the transactions.
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from driver_orchestrator.decision import WaitReason
from driver_orchestrator.evaluate import Result, evaluate_driver
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
LICENCE = "01234567"
TYPES = frozenset({"dmer"})
STATUSES = frozenset({"rejected"})


def _mercury(waiting: int):
    """A fake Mercury lookup returning one driver with *waiting* DMERs."""
    calls = []

    def lookup(licence):
        calls.append(licence)
        return [
            {
                "active_documents": [
                    {"document_type": "DMER", "document_status": "Uploaded"}
                ]
                * waiting
            }
        ]

    lookup.calls = calls
    return lookup


def _mercury_down(_licence):
    raise ConnectionError("Mercury unavailable")


async def _scalar(conn, sql, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


async def _add_document(conn, driver_key, evaluation_id, *, day, outcome, hash_):
    doc = str(
        await _scalar(
            conn,
            "INSERT INTO dmer_document (document_guid, pipeline_status, current_stage, "
            "received_date, driver_key, driver_resolved_by) VALUES (gen_random_uuid(), "
            "'AWAITING_DRIVER_COMPLETION', 'DECISION', :r, :k, 'LICENCE_LOOKUP') "
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
        {"d": doc, "h": hash_, "f": json.dumps({"version": 1, "h": hash_})},
    )
    rank = {"CP": 0, "IN": 1, "PR": 2, "PU": 3}[outcome]
    await conn.execute(
        text(
            "INSERT INTO rule_evaluation (document_id, rules_version, "
            "selected_outcome_code, selected_reason, priority_rank, evaluated_at) "
            "VALUES (:d, 'rules-v1', :c, 'FAKE reason', :p, now())"
        ),
        {"d": doc, "c": outcome, "p": rank},
    )
    await conn.execute(
        text(
            "INSERT INTO driver_evaluation_document (driver_evaluation_id, "
            "document_id, counted_at) VALUES (:e, :d, now())"
        ),
        {"e": evaluation_id, "d": doc},
    )
    return doc


@asynccontextmanager
async def database(documents=(("CP", "h1"), ("CP", "h1"))):
    """A driver with an open evaluation and the given (outcome, hash) documents."""
    schema = "evaluate_test_" + uuid4().hex
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
                    "INSERT INTO driver (licence_number) VALUES (:l) "
                    "RETURNING driver_key",
                    l=LICENCE,
                )
            )
            evaluation_id = str(
                await _scalar(
                    conn,
                    "INSERT INTO driver_evaluation (driver_key, expected_document_count, "
                    "completed_document_count) VALUES (:k, 1, :n) RETURNING id",
                    k=driver_key,
                    n=len(documents),
                )
            )
            docs = [
                await _add_document(
                    conn, driver_key, evaluation_id, day=i + 1, outcome=o, hash_=h
                )
                for i, (o, h) in enumerate(documents)
            ]
        yield engine, driver_key, evaluation_id, docs
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await engine.dispose()


def _evaluate(engine, driver_key, mercury):
    return evaluate_driver(
        engine,
        driver_key,
        mercury_lookup=mercury,
        counted_types=TYPES,
        uncounted_statuses=STATUSES,
    )


async def _evaluation_row(conn, evaluation_id):
    return (
        await conn.execute(
            text(
                "SELECT status, expected_document_count, last_mercury_check_at, "
                "decision_summary FROM driver_evaluation WHERE id = :e"
            ),
            {"e": evaluation_id},
        )
    ).one()


def test_waits_while_mercury_holds_more_dmers():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, _):
            mercury = _mercury(waiting=3)
            result = await _evaluate(engine, driver_key, mercury)
            assert result.result is Result.WAITING
            assert result.wait_reason is WaitReason.WAITING_FOR_DOCUMENTS
            assert (result.expected, result.done) == (3, 2)
            assert mercury.calls == [LICENCE]
            async with engine.connect() as conn:
                status, expected, checked, _ = await _evaluation_row(
                    conn, evaluation_id
                )
                decisions = await _scalar(conn, "SELECT count(*) FROM dmer_decision")
            # refreshed from Mercury, check time recorded, nothing decided
            assert (status, expected, decisions) == ("WAITING", 3, 0)
            assert checked is not None

    asyncio.run(run())


def test_decides_when_complete_and_writes_one_decision_per_document():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, docs):
            result = await _evaluate(engine, driver_key, _mercury(waiting=2))
            assert result.result is Result.DECIDED
            assert result.decided_count == 2
            older, newer = docs
            async with engine.connect() as conn:
                status, expected, _, summary = await _evaluation_row(
                    conn, evaluation_id
                )
                rows = {
                    str(r.document_id): r
                    for r in (
                        await conn.execute(
                            text(
                                "SELECT document_id, outcome_code, is_duplicate, "
                                "duplicate_of_document_id, driver_mapped, "
                                "proposed_driver_key, decision_reason, decided_by, "
                                "driver_evaluation_id FROM dmer_decision"
                            )
                        )
                    ).all()
                }
                doc_statuses = (
                    (
                        await conn.execute(
                            text("SELECT DISTINCT pipeline_status FROM dmer_document")
                        )
                    )
                    .scalars()
                    .all()
                )
            assert (status, expected) == ("DECIDED", 2)
            assert summary["document_count"] == 2
            assert summary["expected_document_count"] == 2
            assert "FAKE reason" not in json.dumps(summary)  # no clinical text
            assert doc_statuses == ["DECIDED"]
            # Clean-pass duplicates: the newest is kept, the older rejected
            assert rows[newer].is_duplicate is False
            assert rows[older].is_duplicate is True
            assert str(rows[older].duplicate_of_document_id) == newer
            # Driver matched from the page's licence: mapping proposed
            assert rows[newer].driver_mapped is False
            assert str(rows[newer].proposed_driver_key) == driver_key
            assert {r.outcome_code for r in rows.values()} == {"CP"}
            assert {r.decided_by for r in rows.values()} == {"AI"}
            assert {str(r.driver_evaluation_id) for r in rows.values()} == {
                evaluation_id
            }
            assert rows[newer].decision_reason["rule_reason"] == "FAKE reason"

    asyncio.run(run())


def test_mercury_down_waits_and_never_decides():
    async def run():
        async with database() as (engine, driver_key, evaluation_id, _):
            result = await _evaluate(engine, driver_key, _mercury_down)
            assert result.result is Result.WAITING
            assert result.wait_reason is WaitReason.MERCURY_UNVERIFIED
            async with engine.connect() as conn:
                status, expected, _, _ = await _evaluation_row(conn, evaluation_id)
            assert status == "WAITING"
            assert expected == 1  # Driver Lookup's count is kept, not cleared

    asyncio.run(run())


def test_a_late_signal_for_a_decided_batch_changes_nothing():
    async def run():
        async with database() as (engine, driver_key, _, _docs):
            first = await _evaluate(engine, driver_key, _mercury(waiting=2))
            second = await _evaluate(engine, driver_key, _mercury(waiting=2))
            assert (first.result, second.result) == (
                Result.DECIDED,
                Result.ALREADY_DECIDED,
            )
            async with engine.connect() as conn:
                assert await _scalar(conn, "SELECT count(*) FROM dmer_decision") == 2

    asyncio.run(run())


def test_waiting_then_decided_on_the_next_signal():
    async def run():
        async with database() as (engine, driver_key, _evaluation_id, _docs):
            assert (
                await _evaluate(engine, driver_key, _mercury(waiting=3))
            ).result is Result.WAITING
            # a person triaged the third DMER in Mercury meanwhile
            assert (
                await _evaluate(engine, driver_key, _mercury(waiting=2))
            ).result is Result.DECIDED

    asyncio.run(run())


def test_differing_documents_all_go_to_intake():
    async def run():
        async with database(documents=(("CP", "h1"), ("PU", "h2"))) as (
            engine,
            driver_key,
            _,
            _docs,
        ):
            await _evaluate(engine, driver_key, _mercury(waiting=2))
            async with engine.connect() as conn:
                outcomes = (
                    (await conn.execute(text("SELECT outcome_code FROM dmer_decision")))
                    .scalars()
                    .all()
                )
            assert sorted(outcomes) == ["IN", "IN"]

    asyncio.run(run())


def test_driver_without_open_evaluation():
    async def run():
        async with database() as (engine, _, _evaluation_id, _docs):
            result = await _evaluate(engine, str(uuid4()), _mercury(waiting=1))
            assert result.result is Result.NO_OPEN_EVALUATION

    asyncio.run(run())
