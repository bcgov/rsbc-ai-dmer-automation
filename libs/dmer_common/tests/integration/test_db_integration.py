"""Integration test: DmerDocumentRepository against PostgreSQL.

Runs against a real PostgreSQL via an async SQLAlchemy engine. Skipped unless
``POSTGRES_TEST_DSN`` is set and an async driver (``asyncpg``) is importable, so
the default unit run is unaffected.

GIVEN a fresh dmer_document table
WHEN a document moves RECEIVED -> DOWNLOADED -> EXTRACTING -> EXTRACTED with the
     combined-extraction blob URL
THEN each status is persisted and read back, and an illegal transition is
     rejected.
"""

from __future__ import annotations

import os

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


def test_status_lifecycle_persists_and_reads_back():
    import asyncio

    asyncio.run(_status_lifecycle())


async def _status_lifecycle():
    from dmer_common.db import PipelineStage, PipelineStatus
    from dmer_common.db.dmer_document import (
        DmerDocumentRepository,
        InvalidStatusTransition,
        dmer_document,
        metadata,
    )
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import create_async_engine

    assert DSN is not None
    engine = create_async_engine(DSN)
    doc_id = "doc-int-db-1"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            # Re-runnable against a reused database: start from no row.
            await conn.execute(
                delete(dmer_document).where(dmer_document.c.id == doc_id)
            )

        repo = DmerDocumentRepository(engine)

        await repo.upsert_status(
            doc_id,
            "case-1",
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid="guid-int-db-1",
            stage=PipelineStage.INGEST,
        )
        await repo.upsert_status(
            doc_id,
            "case-1",
            PipelineStatus.DOWNLOADED,
            expected=PipelineStatus.RECEIVED,
            stage=PipelineStage.EXTRACT,
        )
        await repo.upsert_status(
            doc_id,
            "case-1",
            PipelineStatus.EXTRACTING,
            expected=PipelineStatus.DOWNLOADED,
        )
        assert await repo.get_status(doc_id) == PipelineStatus.EXTRACTING

        # Illegal jump is rejected.
        with pytest.raises(InvalidStatusTransition):
            await repo.upsert_status(
                doc_id,
                "case-1",
                PipelineStatus.DECIDED,
                expected=PipelineStatus.EXTRACTING,
            )

        await repo.upsert_status(
            doc_id,
            "case-1",
            PipelineStatus.EXTRACTED,
            expected=PipelineStatus.EXTRACTING,
            stage=PipelineStage.NORMALIZE,
            extracted_blob_url="extracted-dmer/doc-int-db-1/combined.json",
        )
        assert await repo.get_status(doc_id) == PipelineStatus.EXTRACTED

        # A status-only write (no stage) leaves current_stage where it was.
        await repo.upsert_status(
            doc_id,
            "case-1",
            PipelineStatus.MANUAL_REVIEW,
            expected=PipelineStatus.EXTRACTED,
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
        assert (
            await repo.get_extracted_blob_url(doc_id)
            == "extracted-dmer/doc-int-db-1/combined.json"
        )
        assert await repo.get_extracted_blob_url("no-such-doc") is None
    finally:
        await engine.dispose()


def test_stage_run_attempts_and_abandonment():
    import asyncio

    asyncio.run(_stage_run_attempts())


async def _stage_run_attempts():
    """GIVEN a fresh dmer_stage_run table
    WHEN attempt 1 crashes (left RUNNING), attempt 2 fails, attempt 3 succeeds
    THEN attempts are numbered 1..3, the crashed run is closed ABANDONED, and each
         row carries its own outcome."""
    from dmer_common.db import PipelineStage, StageRunRepository
    from dmer_common.db.stage_run import dmer_stage_run, metadata
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DSN)
    doc_id = "doc-int-stage-run-1"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                delete(dmer_stage_run).where(dmer_stage_run.c.document_id == doc_id)
            )

        runs = StageRunRepository(engine)
        crashed = await runs.start(doc_id, PipelineStage.EXTRACT, model_version="m1")
        failed = await runs.start(doc_id, PipelineStage.EXTRACT, model_version="m1")
        await runs.fail(failed, error_code="RuntimeError")
        ok = await runs.start(doc_id, PipelineStage.EXTRACT, model_version="m2")
        await runs.succeed(ok, output_blob_url="extracted-dmer/doc/combined.json")

        async with engine.connect() as conn:
            rows = {
                r.id: r
                for r in (
                    await conn.execute(
                        select(dmer_stage_run).where(
                            dmer_stage_run.c.document_id == doc_id
                        )
                    )
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
    finally:
        await engine.dispose()


def test_extraction_upsert_is_idempotent_on_document_id():
    import asyncio

    asyncio.run(_extraction_upsert())


async def _extraction_upsert():
    """GIVEN a fresh dmer_extraction table
    WHEN the same document's extraction is upserted twice (a reprocessed run)
    THEN one row exists, holding the second run's values."""
    from dmer_common.db import ExtractionRecord, ExtractionRepository
    from dmer_common.db.dmer_extraction import dmer_extraction, metadata
    from sqlalchemy import delete, select
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DSN)
    doc_id = "doc-int-extraction-1"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                delete(dmer_extraction).where(dmer_extraction.c.document_id == doc_id)
            )

        repo = ExtractionRepository(engine)
        await repo.upsert(
            ExtractionRecord(
                document_id=doc_id,
                licence_number_read="01234567",
                has_header=True,
                has_signature=True,
                is_cutoff=False,
            )
        )
        await repo.upsert(
            ExtractionRecord(
                document_id=doc_id,
                licence_number_read="01234567",
                has_header=True,
                has_signature=False,
                is_cutoff=True,
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
        assert (row.has_header, row.has_signature, row.is_cutoff) == (
            True,
            False,
            True,
        )
    finally:
        await engine.dispose()


def test_status_transitions_are_atomic_compare_and_set():
    import asyncio

    asyncio.run(_compare_and_set())


async def _compare_and_set():
    """GIVEN a document at EXTRACTING
    WHEN two workers race the same EXTRACTING -> EXTRACTED write concurrently
    THEN exactly one succeeds and the other gets StaleStatusError; a stale
         expected status (backwards move) and a duplicate insert are rejected."""
    import asyncio

    from dmer_common.db import PipelineStage, PipelineStatus, StaleStatusError
    from dmer_common.db.dmer_document import (
        DmerDocumentRepository,
        dmer_document,
        metadata,
    )
    from sqlalchemy import delete
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DSN, pool_size=5)
    doc_id = "doc-int-cas-1"
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(
                delete(dmer_document).where(dmer_document.c.id == doc_id)
            )
        repo = DmerDocumentRepository(engine)
        await repo.upsert_status(
            doc_id,
            "c",
            PipelineStatus.RECEIVED,
            expected=None,
            document_guid="guid-int-cas-1",
            stage=PipelineStage.INGEST,
        )
        await repo.upsert_status(
            doc_id, "c", PipelineStatus.DOWNLOADED, expected=PipelineStatus.RECEIVED
        )
        await repo.upsert_status(
            doc_id, "c", PipelineStatus.EXTRACTING, expected=PipelineStatus.DOWNLOADED
        )

        async def worker():
            try:
                await repo.upsert_status(
                    doc_id,
                    "c",
                    PipelineStatus.EXTRACTED,
                    expected=PipelineStatus.EXTRACTING,
                )
                return "won"
            except StaleStatusError as exc:
                assert exc.actual is PipelineStatus.EXTRACTED
                return "lost"

        results = await asyncio.gather(*(worker() for _ in range(5)))
        assert sorted(results) == ["lost"] * 4 + ["won"]
        assert await repo.get_status(doc_id) is PipelineStatus.EXTRACTED

        # A worker still holding the old read cannot move the status backwards.
        with pytest.raises(StaleStatusError):
            await repo.upsert_status(
                doc_id,
                "c",
                PipelineStatus.EXTRACTING,
                expected=PipelineStatus.DOWNLOADED,
            )
        # A second initial insert for the same id loses too.
        with pytest.raises(StaleStatusError):
            await repo.upsert_status(
                doc_id,
                "c",
                PipelineStatus.RECEIVED,
                expected=None,
                document_guid="guid-int-cas-1",
                stage=PipelineStage.INGEST,
            )
        assert await repo.get_status(doc_id) is PipelineStatus.EXTRACTED
    finally:
        await engine.dispose()
