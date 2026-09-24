"""Integration tests: the ``dmer_common.db`` repositories against PostgreSQL.

Runs against a real PostgreSQL with the Flyway schema applied (see
``conftest.py``). Skipped unless ``POSTGRES_TEST_DSN`` is set and ``asyncpg`` is
importable, so the default unit run is unaffected. Every test uses fresh uuids,
so the suite is re-runnable against the same database.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest

DSN = os.getenv("POSTGRES_TEST_DSN")

_has_driver = True
try:  # pragma: no cover - environment dependent
    import asyncpg  # noqa: F401
except Exception:  # noqa: BLE001
    _has_driver = False

pytestmark = pytest.mark.skipif(
    not (DSN and _has_driver),
    reason="PostgreSQL not configured (set POSTGRES_TEST_DSN and install asyncpg)",
)


def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(DSN, poolclass=NullPool)


def _ids() -> tuple[str, str]:
    """A fresh (document id, document guid) pair."""
    return str(uuid.uuid4()), str(uuid.uuid4())


def _run(coro):
    return asyncio.run(coro)


# --- dmer_document: compare-and-set status transitions -----------------------


def test_status_lifecycle_persists_and_reads_back():
    _run(_status_lifecycle())


async def _status_lifecycle():
    from dmer_common.db import PipelineStage, PipelineStatus
    from dmer_common.db.dmer_document import (
        DmerDocumentRepository,
        InvalidStatusTransition,
        dmer_document,
    )
    from sqlalchemy import select

    engine = _engine()
    doc_id, guid = _ids()
    try:
        repo = DmerDocumentRepository(engine)
        await repo.upsert_status(
            doc_id,
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid=guid,
            stage=PipelineStage.INGEST,
        )
        await repo.upsert_status(
            doc_id,
            PipelineStatus.DOWNLOADED,
            expected=PipelineStatus.RECEIVED,
            stage=PipelineStage.EXTRACT,
        )
        await repo.upsert_status(
            doc_id, PipelineStatus.EXTRACTING, expected=PipelineStatus.DOWNLOADED
        )
        assert await repo.get_status(doc_id) == PipelineStatus.EXTRACTING

        # Illegal jump is rejected before any SQL.
        with pytest.raises(InvalidStatusTransition):
            await repo.upsert_status(
                doc_id, PipelineStatus.DECIDED, expected=PipelineStatus.EXTRACTING
            )

        await repo.upsert_status(
            doc_id,
            PipelineStatus.EXTRACTED,
            expected=PipelineStatus.EXTRACTING,
            stage=PipelineStage.NORMALIZE,
        )
        assert await repo.get_status(doc_id) == PipelineStatus.EXTRACTED

        # A status-only write (no stage) leaves current_stage where it was.
        await repo.upsert_status(
            doc_id, PipelineStatus.MANUAL_REVIEW, expected=PipelineStatus.EXTRACTED
        )
        async with engine.connect() as conn:
            stage_now = (
                await conn.execute(
                    select(dmer_document.c.current_stage).where(
                        dmer_document.c.id == doc_id
                    )
                )
            ).scalar_one()
        assert stage_now == "NORMALIZE"
        assert await repo.get_status(str(uuid.uuid4())) is None
    finally:
        await engine.dispose()


def test_status_transitions_are_atomic_compare_and_set():
    _run(_compare_and_set())


async def _compare_and_set():
    """GIVEN a document at EXTRACTING
    WHEN five workers race the same EXTRACTING -> EXTRACTED write concurrently
    THEN exactly one succeeds and the others get StaleStatusError; a stale
         expected status (backwards move) and a duplicate insert are rejected."""
    from dmer_common.db import (
        DmerDocumentRepository,
        PipelineStage,
        PipelineStatus,
        StaleStatusError,
    )

    engine = _engine()
    doc_id, guid = _ids()
    try:
        repo = DmerDocumentRepository(engine)
        await repo.upsert_status(
            doc_id,
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid=guid,
            stage=PipelineStage.INGEST,
        )
        await repo.upsert_status(
            doc_id, PipelineStatus.DOWNLOADED, expected=PipelineStatus.RECEIVED
        )
        await repo.upsert_status(
            doc_id, PipelineStatus.EXTRACTING, expected=PipelineStatus.DOWNLOADED
        )

        async def worker():
            try:
                await repo.upsert_status(
                    doc_id, PipelineStatus.EXTRACTED, expected=PipelineStatus.EXTRACTING
                )
                return "won"
            except StaleStatusError as exc:
                assert exc.actual is PipelineStatus.EXTRACTED
                return "lost"

        results = await asyncio.gather(*(worker() for _ in range(5)))
        assert sorted(results) == ["lost"] * 4 + ["won"]

        with pytest.raises(StaleStatusError):
            await repo.upsert_status(
                doc_id, PipelineStatus.EXTRACTING, expected=PipelineStatus.DOWNLOADED
            )
        with pytest.raises(StaleStatusError):
            await repo.upsert_status(
                doc_id,
                PipelineStatus.RECEIVED,
                expected=None,
                document_guid=guid,
                stage=PipelineStage.INGEST,
            )
        assert await repo.get_status(doc_id) is PipelineStatus.EXTRACTED
    finally:
        await engine.dispose()


# --- Ingest -> Extraction handoff on the real schema --------------------------


def test_ingest_row_hands_off_to_extraction():
    _run(_ingest_handoff())


async def _ingest_handoff():
    """GIVEN Ingest created a document (upsert_received) and downloaded it
    (mark_downloaded) WHEN Extraction moves it on with compare-and-set
    THEN DOWNLOADED -> EXTRACTING -> EXTRACTED succeeds on Ingest's row, and a
         re-poll of the same document doesn't reset it."""
    from dmer_common.db import DmerDocumentRepository, PipelineStage, PipelineStatus

    engine = _engine()
    guid = str(uuid.uuid4())
    now = datetime.now(UTC)
    try:
        repo = DmerDocumentRepository(engine)
        fields = {
            "document_guid": guid,
            "document_name": "dmer.pdf",
            "mercury_document_status": "Uploaded",
            "document_priority": None,
            "received_date": now,
            "dps_date": None,
            "queue": "DPS General",
            "business_area": None,
            "mercury_case_id": None,
            "driver_key": None,
            "document_url": "https://mercury.example/presigned",
            "now": now,
        }
        doc_id = await repo.upsert_received(**fields)
        await repo.mark_downloaded(
            doc_id, raw_blob_url="https://acct/raw-dmer/x.pdf", now=datetime.now(UTC)
        )
        assert await repo.get_status(doc_id) is PipelineStatus.DOWNLOADED

        await repo.upsert_status(
            doc_id,
            PipelineStatus.EXTRACTING,
            expected=PipelineStatus.DOWNLOADED,
            stage=PipelineStage.EXTRACT,
        )
        await repo.upsert_status(
            doc_id,
            PipelineStatus.EXTRACTED,
            expected=PipelineStatus.EXTRACTING,
            stage=PipelineStage.NORMALIZE,
        )
        row = await repo.get_by_id(doc_id)
        assert (row.pipeline_status, row.current_stage) == ("EXTRACTED", "NORMALIZE")

        # Ingest re-polls the same document: DO NOTHING on document_guid
        assert await repo.upsert_received(**fields) == doc_id
        assert await repo.get_status(doc_id) is PipelineStatus.EXTRACTED
    finally:
        await engine.dispose()


def test_driver_upsert_uses_the_canonical_bc_licence():
    _run(_driver_bc_licence())


async def _driver_bc_licence():
    """GIVEN Mercury's licence as a 7-digit number WHEN Ingest upserts the driver
    THEN it is stored in the canonical 8-digit form Extraction also uses, so a
    page-read licence finds the same driver."""
    import random

    from dmer_common.db import DriverRepository
    from dmer_common.licence import normalize_licence

    engine = _engine()
    seven = f"{random.randrange(1_000_000, 9_999_999)}"  # fresh 7-digit number
    try:
        repo = DriverRepository(engine)
        key = await repo.upsert(
            seven,
            mercury_driver_id=None,
            first_name=None,
            last_name=None,
            last_synced_at=datetime.now(UTC),
        )
        found = await repo.get_by_licence_number(normalize_licence(seven))
        assert found is not None
        assert found.driver_key == key
        assert found.licence_number == "0" + seven
        assert await repo.get_by_licence_number("not-a-bc-licence") is None
    finally:
        await engine.dispose()


# --- dmer_stage_run ------------------------------------------------------------


def test_stage_run_attempts_and_abandonment():
    _run(_stage_run_attempts())


async def _stage_run_attempts():
    """GIVEN a document WHEN attempt 1 crashes (left RUNNING), attempt 2 fails,
    attempt 3 succeeds THEN attempts are numbered 1..3, the crashed run is closed
    ABANDONED, and each row carries its own outcome. An explicit attempt_no (as
    Ingest passes) is honoured."""
    from dmer_common.db import (
        DmerDocumentRepository,
        DmerStageRunRepository,
        PipelineStage,
        PipelineStatus,
    )
    from dmer_common.db.dmer_stage_run import dmer_stage_run
    from sqlalchemy import select

    engine = _engine()
    doc_id, guid = _ids()
    try:
        await DmerDocumentRepository(engine).upsert_status(
            doc_id,
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid=guid,
            stage=PipelineStage.EXTRACT,
        )
        runs = DmerStageRunRepository(engine)
        crashed = await runs.start(
            document_id=doc_id, stage=PipelineStage.EXTRACT, model_version="m1"
        )
        failed = await runs.start(
            document_id=doc_id, stage=PipelineStage.EXTRACT, model_version="m1"
        )
        await runs.fail(failed, error_code="RuntimeError")
        ok = await runs.start(
            document_id=doc_id, stage=PipelineStage.EXTRACT, model_version="m2"
        )
        await runs.succeed(ok, output_blob_url="extracted-dmer/doc/combined.json")
        explicit = await runs.start(
            document_id=doc_id,
            stage="INGEST",
            attempt_no=7,
            started_at=datetime.now(UTC),
        )

        async with engine.connect() as conn:
            rows = {
                r.id: r
                for r in await conn.execute(
                    select(dmer_stage_run).where(dmer_stage_run.c.document_id == doc_id)
                )
            }

        assert [rows[i].attempt_no for i in (crashed, failed, ok)] == [1, 2, 3]
        assert (rows[crashed].status, rows[crashed].error_code) == (
            "FAILED",
            "ABANDONED",
        )
        assert rows[crashed].ended_at is not None
        assert (rows[failed].status, rows[failed].error_code) == (
            "FAILED",
            "RuntimeError",
        )
        assert rows[ok].status == "SUCCEEDED"
        assert rows[ok].output_blob_url == "extracted-dmer/doc/combined.json"
        assert rows[ok].model_version == "m2"
        assert (rows[explicit].stage, rows[explicit].attempt_no) == ("INGEST", 7)
    finally:
        await engine.dispose()


# --- dmer_extraction -------------------------------------------------------------


def test_extraction_upsert_is_idempotent_on_document_id():
    _run(_extraction_upsert())


async def _extraction_upsert():
    """GIVEN a document WHEN its extraction is upserted twice (a reprocessed run)
    THEN one row exists, holding the second run's values."""
    from dmer_common.db import (
        DmerDocumentRepository,
        ExtractionRecord,
        ExtractionRepository,
        PipelineStage,
        PipelineStatus,
    )
    from dmer_common.db.dmer_extraction import dmer_extraction
    from sqlalchemy import select

    engine = _engine()
    doc_id, guid = _ids()
    try:
        await DmerDocumentRepository(engine).upsert_status(
            doc_id,
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid=guid,
            stage=PipelineStage.EXTRACT,
        )
        repo = ExtractionRepository(engine)
        for signature in (True, False):
            await repo.upsert(
                ExtractionRecord(
                    document_id=doc_id,
                    licence_number_read="01234567",
                    has_header=True,
                    has_signature=signature,
                    is_cutoff=not signature,
                )
            )

        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(dmer_extraction).where(
                        dmer_extraction.c.document_id == doc_id
                    )
                )
            ).all()
        assert len(rows) == 1
        row = rows[0]
        assert row.licence_number_read == "01234567"
        assert (row.has_header, row.has_signature, row.is_cutoff) == (True, False, True)
    finally:
        await engine.dispose()
