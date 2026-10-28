"""The Outbox Publisher against an isolated PostgreSQL schema.

Set NORMALIZATION_TEST_DSN to a disposable PostgreSQL database. Mercury is a
fake that records the calls; the rows come from the real Post-Processing
builders.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dmer_common.db.mercury_outbox import (
    decision_rows,
    fallback_rows,
    queue_outbox_rows,
)
from dmer_common.db.outbox_publisher import PublisherSettings, claim_due, publish_due
from dmer_common.mercury_client import MercuryWriteError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = pytest.mark.skipif(
    not os.getenv("NORMALIZATION_TEST_DSN"), reason="requires disposable PostgreSQL"
)
_MIGRATIONS = Path(__file__).resolve().parents[4] / "database/migrations"
_SCHEMA = sorted(_MIGRATIONS.glob("V*.sql"), key=lambda path: int(path.name[1:5]))
T0 = datetime(2026, 10, 16, 12, tzinfo=UTC)
SETTINGS = PublisherSettings(batch_size=10, max_attempts=3)


class FakeMercury:
    """Records (operation, key); fails per operation from *failures*."""

    def __init__(self, failures=None):
        self.calls = []
        self.failures = dict(failures or {})

    def send_outbox(self, operation, payload, key):
        self.calls.append((operation, key))
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure
        return {"ok": True, "operation": operation}


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now


async def _scalar(conn, sql, **params):
    return (await conn.execute(text(sql), params)).scalar_one()


@asynccontextmanager
async def database():
    schema = "outbox_test_" + uuid4().hex
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


async def _decided_batch(engine, *, duplicate=False):
    """A DECIDED evaluation with two POSTING documents and their outbox rows
    (the second a rejected duplicate when *duplicate*). Returns ids."""
    async with engine.begin() as conn:
        driver_key = str(
            await _scalar(
                conn,
                "INSERT INTO driver (licence_number, mercury_driver_id) "
                "VALUES ('01234567', 'D7') RETURNING driver_key",
            )
        )
        evaluation = str(
            await _scalar(
                conn,
                "INSERT INTO driver_evaluation (driver_key, status, open, "
                "completed_document_count) VALUES (:k, 'DECIDED', false, 2) RETURNING id",
                k=driver_key,
            )
        )
        docs = []
        for _ in range(2):
            doc = await conn.execute(
                text(
                    "INSERT INTO dmer_document (document_guid, pipeline_status, "
                    "current_stage, driver_key) VALUES (gen_random_uuid(), 'POSTING', "
                    "'POST', :k) RETURNING id::text, document_guid::text"
                ),
                {"k": driver_key},
            )
            docs.append(tuple(doc.one()))
        rows = []
        for i, (doc, guid) in enumerate(docs):
            is_dup = duplicate and i == 1
            decision = str(
                await _scalar(
                    conn,
                    "INSERT INTO dmer_decision (document_id, driver_evaluation_id, "
                    "outcome_code, is_duplicate, decided_by) VALUES "
                    "(:d, :e, 'CP', :dup, 'AI') RETURNING id",
                    d=doc,
                    e=evaluation,
                    dup=is_dup,
                )
            )
            rows += decision_rows(
                decision_id=decision,
                document_id=doc,
                document_guid=guid,
                outcome_code="CP",
                decided_by="AI",
                reason={"rule_reason": "FAKE"},
                mercury_driver_id="D7",
                is_duplicate=is_dup,
                duplicate_of_document_guid=docs[0][1] if is_dup else None,
            )
        await queue_outbox_rows(conn, rows, now=T0)
    return evaluation, [d for d, _ in docs]


async def _statuses(engine):
    async with engine.connect() as conn:
        outbox = (
            await conn.execute(
                text(
                    "SELECT document_id::text, operation::text, status::text, "
                    "attempt_count, next_attempt_at, last_error FROM mercury_outbox"
                )
            )
        ).all()
        documents = dict(
            (
                await conn.execute(
                    text("SELECT id::text, pipeline_status::text FROM dmer_document")
                )
            ).all()
        )
        evaluations = (
            (await conn.execute(text("SELECT status::text FROM driver_evaluation")))
            .scalars()
            .all()
        )
    return outbox, documents, evaluations


def test_a_decided_batch_is_sent_in_order_and_completes():
    async def run():
        async with database() as engine:
            await _decided_batch(engine, duplicate=True)
            mercury = FakeMercury()
            summary = await publish_due(
                engine, mercury, settings=SETTINGS, clock=Clock()
            )
            outbox, documents, evaluations = await _statuses(engine)
            assert (summary.documents, summary.sent, summary.failed) == (2, 5, 0)
            # per document: the case before the duplicate before the outcome
            ops = [op for op, _ in mercury.calls]
            first, second = ops[:2], ops[2:]
            assert {tuple(first), tuple(second)} == {
                ("CREATE_CASE", "UPDATE_OUTCOME"),
                ("CREATE_CASE", "MARK_DUPLICATE", "UPDATE_OUTCOME"),
            }
            assert {r.status for r in outbox} == {"SENT"}
            assert {r.attempt_count for r in outbox} == {1}
            assert set(documents.values()) == {"COMPLETED"}
            assert evaluations == ["POSTED"]
            assert summary.evaluations_posted == 1
            # nothing left: a second run sends nothing
            again = await publish_due(engine, mercury, settings=SETTINGS, clock=Clock())
            assert again.documents == 0 and len(mercury.calls) == 5

    asyncio.run(run())


def test_a_mercury_outage_backs_off_and_recovers_with_nothing_lost():
    async def run():
        async with database() as engine:
            await _decided_batch(engine)
            outage = MercuryWriteError("HTTP 503", status=503, retryable=True)
            mercury = FakeMercury({"CREATE_CASE": outage})
            clock = Clock()
            summary = await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            assert (summary.sent, summary.retrying) == (0, 2)
            # the outcome is never sent before its case
            assert {op for op, _ in mercury.calls} == {"CREATE_CASE"}
            outbox, documents, evaluations = await _statuses(engine)
            assert {r.status for r in outbox} == {"PENDING"}
            cases = [r for r in outbox if r.operation == "CREATE_CASE"]
            assert {(r.attempt_count, r.next_attempt_at) for r in cases} == {
                (1, T0 + timedelta(minutes=1))
            }
            # the outcome waits for the case's next attempt
            outcomes = [r for r in outbox if r.operation == "UPDATE_OUTCOME"]
            assert {(r.attempt_count, r.next_attempt_at) for r in outcomes} == {
                (0, T0 + timedelta(minutes=1))
            }
            assert set(documents.values()) == {"POSTING"}
            # not due yet: nothing is retried early
            clock.now = T0 + timedelta(seconds=30)
            assert (
                await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            ).documents == 0
            # Mercury is back
            mercury.failures.clear()
            clock.now = T0 + timedelta(minutes=2)
            summary = await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            assert summary.sent == 4
            outbox, documents, evaluations = await _statuses(engine)
            assert {r.status for r in outbox} == {"SENT"}
            assert set(documents.values()) == {"COMPLETED"}
            assert evaluations == ["POSTED"]

    asyncio.run(run())


def test_a_rejected_write_fails_the_row_and_blocks_the_rest_of_the_document():
    async def run():
        async with database() as engine:
            _, docs = await _decided_batch(engine)
            rejected = MercuryWriteError("HTTP 400", status=400, retryable=False)
            mercury = FakeMercury({"CREATE_CASE": rejected})
            summary = await publish_due(
                engine, mercury, settings=SETTINGS, clock=Clock()
            )
            assert summary.failed == 2
            outbox, documents, evaluations = await _statuses(engine)
            by_op = {(r.document_id, r.operation): r for r in outbox}
            for doc in docs:
                case, outcome = (
                    by_op[(doc, "CREATE_CASE")],
                    by_op[(doc, "UPDATE_OUTCOME")],
                )
                assert (case.status, case.last_error) == ("FAILED", "HTTP 400")
                assert (outcome.status, outcome.last_error) == (
                    "FAILED",
                    "blocked: CREATE_CASE failed",
                )
                assert outcome.next_attempt_at is None
            # visible, not completed
            assert set(documents.values()) == {"POSTING"}
            assert evaluations == ["DECIDED"]

    asyncio.run(run())


def test_a_row_fails_after_max_attempts():
    async def run():
        async with database() as engine:
            await _decided_batch(engine)
            outage = MercuryWriteError("HTTP 503", status=503, retryable=True)
            mercury = FakeMercury({"CREATE_CASE": outage})
            clock = Clock()
            for attempt in range(SETTINGS.max_attempts):
                clock.now = T0 + timedelta(hours=2 * attempt)
                await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            outbox, _, _ = await _statuses(engine)
            cases = [r for r in outbox if r.operation == "CREATE_CASE"]
            assert {(r.status, r.attempt_count) for r in cases} == {
                ("FAILED", SETTINGS.max_attempts)
            }

    asyncio.run(run())


def test_claimed_rows_are_leased_so_a_second_run_does_not_take_them():
    async def run():
        async with database() as engine:
            await _decided_batch(engine)
            first = await claim_due(engine, now=T0, settings=SETTINGS)
            second = await claim_due(engine, now=T0, settings=SETTINGS)
            assert len(first) == 2 and second == {}
            # a run that died mid-send: the lease expires and the rows come back
            later = await claim_due(
                engine,
                now=T0 + SETTINGS.lease + timedelta(seconds=1),
                settings=SETTINGS,
            )
            assert set(later) == set(first)

    asyncio.run(run())


def test_a_fallback_outcome_is_sent_and_the_document_stays_in_manual_review():
    async def run():
        async with database() as engine:
            async with engine.begin() as conn:
                doc, guid = (
                    await conn.execute(
                        text(
                            "INSERT INTO dmer_document (document_guid, pipeline_status, "
                            "current_stage) VALUES (gen_random_uuid(), 'MANUAL_REVIEW', "
                            "'DRIVER_LOOKUP') RETURNING id::text, document_guid::text"
                        )
                    )
                ).one()
                await queue_outbox_rows(
                    conn,
                    fallback_rows(
                        source_id=f"{doc}:FALLBACK",
                        document_id=doc,
                        document_guid=guid,
                        comment="AI could not process. FAKE",
                    ),
                    now=T0,
                )
            mercury = FakeMercury()
            summary = await publish_due(
                engine, mercury, settings=SETTINGS, clock=Clock()
            )
            assert mercury.calls == [
                ("UPDATE_OUTCOME", f"{doc}:FALLBACK:UPDATE_OUTCOME")
            ]
            assert summary.sent == 1 and summary.documents_completed == 0
            outbox, documents, _ = await _statuses(engine)
            assert [r.status for r in outbox] == ["SENT"]
            assert documents[doc] == "MANUAL_REVIEW"

    asyncio.run(run())
