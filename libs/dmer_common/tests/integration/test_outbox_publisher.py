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
    ProposedDriver,
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

    def __init__(self, failures=None, responses=None):
        self.calls = []
        self.payloads = []
        self.failures = dict(failures or {})
        self.responses = dict(responses or {})

    def send_outbox(self, operation, payload, key):
        self.calls.append((operation, key))
        self.payloads.append((operation, dict(payload)))
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure
        return self.responses.get(operation, {"ok": True, "operation": operation})


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


async def _decided_batch(engine, *, duplicate=False, proposed_driver=False):
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
                proposed_driver=proposed_driver,
                proposed_mercury_driver_id="D7" if proposed_driver else None,
                proposed_driver_details=(
                    ProposedDriver("01234567", "FAKE", "DRIVER")
                    if proposed_driver
                    else None
                ),
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
            assert (summary.documents, summary.sent, summary.failed) == (2, 4, 0)
            # per document: the case before the outcome (a duplicate is
            # flagged on its outcome row)
            ops = [op for op, _ in mercury.calls]
            assert ops == ["CREATE_CASE", "UPDATE_OUTCOME"] * 2
            assert {r.status for r in outbox} == {"SENT"}
            assert {r.attempt_count for r in outbox} == {1}
            assert set(documents.values()) == {"COMPLETED"}
            assert evaluations == ["POSTED"]
            assert summary.evaluations_posted == 1
            # nothing left: a second run sends nothing
            again = await publish_due(engine, mercury, settings=SETTINGS, clock=Clock())
            assert again.documents == 0 and len(mercury.calls) == 4

    asyncio.run(run())


def test_a_failed_driver_link_still_sends_the_outcome_as_an_error():
    # add_driver rejected for good: the outcome still goes (as a processing
    # error for Intake, the error in its notes); only the case is blocked.
    async def run():
        async with database() as engine:
            await _decided_batch(engine, proposed_driver=True)
            rejected = MercuryWriteError(
                "Mercury write POST rejected: Driver not found",
                status=200,
                retryable=False,
            )
            mercury = FakeMercury({"MAP_DRIVER": rejected})
            summary = await publish_due(
                engine, mercury, settings=SETTINGS, clock=Clock()
            )
            assert [op for op, _ in mercury.calls] == [
                "MAP_DRIVER",
                "UPDATE_OUTCOME",
            ] * 2
            outcomes = [p for op, p in mercury.payloads if op == "UPDATE_OUTCOME"]
            assert {p["driver_link_error"] for p in outcomes} == {
                "Mercury write POST rejected: Driver not found"
            }
            assert (summary.sent, summary.failed) == (2, 2)
            outbox, documents, evaluations = await _statuses(engine)
            by_op = {}
            for _, op, status, *_rest, last_error in outbox:
                by_op.setdefault(op, set()).add((status, last_error))
            assert by_op["UPDATE_OUTCOME"] == {("SENT", None)}
            assert by_op["CREATE_CASE"] == {("FAILED", "blocked: MAP_DRIVER failed")}
            assert {s for s, _ in by_op["MAP_DRIVER"]} == {"FAILED"}
            # handed to Intake: manual review, and the batch counts as posted
            assert set(documents.values()) == {"MANUAL_REVIEW"}
            assert evaluations == ["POSTED"]
            assert summary.documents_manual_review == 2
            assert summary.documents_completed == 0
            # the saved payload keeps the error, so a later retry sends the same
            async with engine.connect() as conn:
                saved = (
                    (
                        await conn.execute(
                            text(
                                "SELECT payload FROM mercury_outbox "
                                "WHERE operation = 'UPDATE_OUTCOME'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert all("driver_link_error" in p for p in saved)

    asyncio.run(run())


def test_an_error_outcome_retried_later_still_hands_the_document_to_intake():
    # The driver link fails for good, then the outcome itself hits an outage:
    # the document waits in POSTING; once the outcome goes (next run, from the
    # saved payload), it moves to MANUAL_REVIEW.
    async def run():
        async with database() as engine:
            await _decided_batch(engine, proposed_driver=True)
            rejected = MercuryWriteError("rejected", status=400, retryable=False)
            outage = MercuryWriteError("HTTP 503", status=503, retryable=True)
            mercury = FakeMercury({"MAP_DRIVER": rejected, "UPDATE_OUTCOME": outage})
            clock = Clock()
            await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            _, documents, evaluations = await _statuses(engine)
            assert set(documents.values()) == {"POSTING"}
            assert evaluations == ["DECIDED"]

            mercury.failures = {}
            clock.now += timedelta(hours=1)
            summary = await publish_due(engine, mercury, settings=SETTINGS, clock=clock)
            sent = [p for op, p in mercury.payloads[-2:] if op == "UPDATE_OUTCOME"]
            assert len(sent) == 2 and all("driver_link_error" in p for p in sent)
            _, documents, evaluations = await _statuses(engine)
            assert set(documents.values()) == {"MANUAL_REVIEW"}
            assert evaluations == ["POSTED"]
            assert summary.documents_manual_review == 2

    asyncio.run(run())


def test_a_failed_case_still_sends_the_outcome_as_usual_with_the_error():
    async def run():
        async with database() as engine:
            await _decided_batch(engine)
            conflict = MercuryWriteError(
                "Mercury write POST returned HTTP 409", status=409, retryable=False
            )
            mercury = FakeMercury({"CREATE_CASE": conflict})
            summary = await publish_due(
                engine, mercury, settings=SETTINGS, clock=Clock()
            )
            assert [op for op, _ in mercury.calls] == [
                "CREATE_CASE",
                "UPDATE_OUTCOME",
            ] * 2
            outcomes = [p for op, p in mercury.payloads if op == "UPDATE_OUTCOME"]
            assert all(
                p["case_error"] == "Mercury write POST returned HTTP 409"
                and "driver_link_error" not in p
                for p in outcomes
            )
            assert (summary.sent, summary.failed) == (2, 2)
            outbox, documents, evaluations = await _statuses(engine)
            assert {(r[1], r[2]) for r in outbox} == {
                ("CREATE_CASE", "FAILED"),
                ("UPDATE_OUTCOME", "SENT"),
            }
            # the case is for a person to resolve: the documents stay POSTING
            assert set(documents.values()) == {"POSTING"}
            assert evaluations == ["DECIDED"]
            assert summary.documents_manual_review == 0

    asyncio.run(run())


@pytest.mark.parametrize(
    "open_case",
    [None, {"caseId": "C0", "caseTitle": "00000000- DFC- DMER- 1"}],
)
def test_add_drivers_answer_decides_the_case(open_case):
    # The case write after a driver link carries add_driver's answer about the
    # driver's active case (saved on the row, so a retry sends it the same way).
    async def run():
        async with database() as engine:
            await _decided_batch(engine, proposed_driver=True)
            linked = {"resultMessage": "linked", "driverOpenCase": open_case}
            mercury = FakeMercury(responses={"MAP_DRIVER": linked})
            await publish_due(engine, mercury, settings=SETTINGS, clock=Clock())
            sent = [p for op, p in mercury.payloads if op == "CREATE_CASE"]
            assert len(sent) == 2
            assert all(
                p["driver_linked"] is True and p["driver_open_case"] == open_case
                for p in sent
            )
            async with engine.connect() as conn:
                saved = (
                    (
                        await conn.execute(
                            text(
                                "SELECT payload FROM mercury_outbox "
                                "WHERE operation = 'CREATE_CASE'"
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
            assert all(p["driver_linked"] is True for p in saved)

    asyncio.run(run())


def test_while_the_driver_link_is_retried_the_outcome_waits():
    async def run():
        async with database() as engine:
            await _decided_batch(engine, proposed_driver=True)
            outage = MercuryWriteError("HTTP 503", status=503, retryable=True)
            mercury = FakeMercury({"MAP_DRIVER": outage})
            await publish_due(engine, mercury, settings=SETTINGS, clock=Clock())
            assert [op for op, _ in mercury.calls] == ["MAP_DRIVER"] * 2
            outbox, _, _ = await _statuses(engine)
            assert {r[2] for r in outbox} == {"PENDING"}

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
                        comment="Unable to AI process due to error. FAKE",
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
