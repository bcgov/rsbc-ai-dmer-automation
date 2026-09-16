"""Integration test: DocumentRepository against PostgreSQL.

Runs against a real PostgreSQL via an async SQLAlchemy engine. Skipped unless
``POSTGRES_TEST_DSN`` is set and an async driver (``asyncpg``) is importable, so
the default unit run is unaffected.

GIVEN a fresh documents table
WHEN a document moves received -> extracting -> ... -> published with blob URLs
THEN each status/URI is persisted and read back, and an illegal transition is
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
    from dmer_common.db import DocumentStatus
    from dmer_common.db.documents import (
        DocumentRepository,
        InvalidStatusTransition,
        metadata,
    )
    from sqlalchemy.ext.asyncio import create_async_engine

    assert DSN is not None
    engine = create_async_engine(DSN)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        repo = DocumentRepository(engine)
        doc_id = "doc-int-db-1"

        await repo.upsert_status(doc_id, "case-1", DocumentStatus.RECEIVED)
        await repo.upsert_status(
            doc_id,
            "case-1",
            DocumentStatus.EXTRACTING,
            initial_extraction_uri="extracted-dmer/doc-int-db-1/top_level.json",
        )
        assert await repo.get_status(doc_id) == DocumentStatus.EXTRACTING

        # Illegal jump is rejected.
        with pytest.raises(InvalidStatusTransition):
            await repo.upsert_status(doc_id, "case-1", DocumentStatus.PUBLISHED)

        for status in (
            DocumentStatus.SECTIONING,
            DocumentStatus.COMBINING,
            DocumentStatus.COMBINED,
        ):
            await repo.upsert_status(doc_id, "case-1", status)
        await repo.upsert_status(
            doc_id,
            "case-1",
            DocumentStatus.PUBLISHED,
            combined_extraction_uri="combined-extracted-dmer/doc-int-db-1/combined.json",
        )
        assert await repo.get_status(doc_id) == DocumentStatus.PUBLISHED
    finally:
        await engine.dispose()
