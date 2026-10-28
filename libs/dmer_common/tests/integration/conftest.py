"""Shared setup for the PostgreSQL integration tests.

When ``POSTGRES_TEST_DSN`` is set, the real Flyway migrations in
``database/migrations/`` (``V*.sql``, in version order) are applied once per test
session, so the repositories are tested against the same schema production
runs — enum types, uuid keys, constraints — not a ``metadata.create_all``
approximation. Every migration is written to be re-runnable (``IF NOT EXISTS`` /
``IF EXISTS``), so a reused database is fine.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re

import pytest

DSN = os.getenv("POSTGRES_TEST_DSN")
MIGRATIONS = pathlib.Path(__file__).resolve().parents[4] / "database" / "migrations"


def _version(path: pathlib.Path) -> int:
    match = re.match(r"V(\d+)__", path.name)
    return int(match.group(1)) if match else 0


@pytest.fixture(scope="session", autouse=True)
def _flyway_schema():
    """Apply ``database/migrations/V*.sql`` to the test database (if configured)."""
    if not DSN:
        return
    try:
        import asyncpg
    except ImportError:  # pragma: no cover - environment dependent
        return

    async def migrate() -> None:
        conn = await asyncpg.connect(
            DSN.replace("postgresql+asyncpg://", "postgresql://")
        )
        try:
            for script in sorted(MIGRATIONS.glob("V*.sql"), key=_version):
                await conn.execute(script.read_text(encoding="utf-8"))
        finally:
            await conn.close()

    asyncio.run(migrate())
