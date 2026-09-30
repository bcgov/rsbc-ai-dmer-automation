"""``driver`` table repository.

The grouping key, independent of Mercury, because documents must be grouped
by licence even when Mercury returns no driver object (see
``docs/development/data-model.md``). ``driver_key`` is what queue messages
and downstream tables use — the licence number itself must never be put on
a queue message (security requirement, architecture doc §9.2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Column, DateTime, MetaData, Table, Text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..licence import normalize_licence

metadata = MetaData()

driver = Table(
    "driver",
    metadata,
    Column(
        "driver_key",
        PG_UUID(as_uuid=False),
        primary_key=True,
        server_default="gen_random_uuid()",
    ),
    Column("licence_number", Text, unique=True, nullable=False),
    Column("mercury_driver_id", Text, nullable=True),
    Column("first_name", Text, nullable=True),
    Column("last_name", Text, nullable=True),
    Column("last_synced_at", DateTime(timezone=True), nullable=True),
)


def normalize_licence_number(raw: str) -> str:
    """Canonical licence number -- the form every uniqueness check and lookup
    in this table uses (see 01-ingest.md's Page Poller step 4).

    Delegates to :func:`dmer_common.licence.normalize_licence`, the single BC
    rule shared with Extraction (``dmer_extraction.licence_number_read``): digits
    only, 7 or 8 long, 7-digit numbers zero-padded to 8. Using one rule is what
    lets a page-read licence match the driver Ingest created. A BC driver
    always has a BC licence number, so anything else is bad source data and
    raises :class:`ValueError` rather than being stored in a form that can
    never match.
    """
    normalized = normalize_licence(raw)
    if normalized is None:
        raise ValueError("not a valid BC driver's licence number")
    return normalized


@dataclass(frozen=True)
class DriverRecord:
    """A row in the ``driver`` table."""

    driver_key: str
    licence_number: str
    mercury_driver_id: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    last_synced_at: datetime | None = None


class DriverRepository:
    """Async repository over the ``driver`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(
        self,
        licence_number: str,
        *,
        mercury_driver_id: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
        last_synced_at: datetime | None = None,
    ) -> str:
        """Insert or update a driver row keyed on the normalized licence
        number, returning ``driver_key`` (existing or newly generated).

        ``INSERT ... ON CONFLICT (licence_number) DO UPDATE`` -- unlike
        ``dmer_document``'s ``DO NOTHING``, this table's data (name, Mercury
        id) is expected to be refreshed on every sighting, so overwriting is
        correct here (see 01-ingest.md).
        """
        normalized = normalize_licence_number(licence_number)
        values = {
            "licence_number": normalized,
            "mercury_driver_id": mercury_driver_id,
            "first_name": first_name,
            "last_name": last_name,
            "last_synced_at": last_synced_at,
        }
        stmt = pg_insert(driver).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[driver.c.licence_number],
            set_={k: v for k, v in values.items() if k != "licence_number"},
        ).returning(driver.c.driver_key)
        async with self._engine.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalar_one()

    async def get_by_licence_number(self, licence_number: str) -> DriverRecord | None:
        """Return the driver row for a (not-yet-normalized) licence number, if any."""
        from sqlalchemy import select

        try:
            normalized = normalize_licence_number(licence_number)
        except ValueError:
            return None  # not a valid BC licence: no driver can match it
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(
                    driver.c.driver_key,
                    driver.c.licence_number,
                    driver.c.mercury_driver_id,
                    driver.c.first_name,
                    driver.c.last_name,
                    driver.c.last_synced_at,
                ).where(driver.c.licence_number == normalized)
            )
            row = result.first()
        return DriverRecord(*row) if row else None
