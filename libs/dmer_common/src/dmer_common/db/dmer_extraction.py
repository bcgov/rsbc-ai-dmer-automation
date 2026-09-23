"""``dmer_extraction`` table repository (revised architecture).

One row per document (1:1 with ``dmer_document``) — the extracted values that need
to be *queried*, not merely stored (the full extraction JSON lives in the
``extracted-dmer`` blob). See ``docs/development/data-model.md`` §``dmer_extraction``.

The three cut-off flags (``has_header`` / ``has_signature`` / ``is_cutoff``) are
deliberately kept separate, not collapsed — Intake needs to know which half of the
form is missing. ``comparison_hash`` is a sha256 of the canonicalized comparison
subset, indexed so downstream duplicate detection is a hash lookup.

The upsert is keyed on ``document_id`` so a redelivered extraction (replay after a
lock-renewal failure) overwrites the row rather than creating a duplicate — the
idempotency requirement in ``docs/development/stages/02-extraction.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Boolean,
    Column,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

dmer_extraction = Table(
    "dmer_extraction",
    metadata,
    Column("document_id", String, primary_key=True),
    Column("licence_number_read", String, nullable=True),
    Column("exam_date", String, nullable=True),
    Column("physician_name", String, nullable=True),
    Column("has_header", Boolean, nullable=True),
    Column("has_signature", Boolean, nullable=True),
    Column("is_cutoff", Boolean, nullable=True),
    Column("page_count", Integer, nullable=True),
    Column("confidence_avg", Numeric, nullable=True),
    Column("comparison_fields", JSONB, nullable=True),
    Column("comparison_hash", String(64), nullable=True),
)


@dataclass(frozen=True)
class ExtractionRecord:
    """Queryable extraction values for one document."""

    document_id: str
    licence_number_read: str | None = None
    exam_date: str | None = None
    physician_name: str | None = None
    has_header: bool | None = None
    has_signature: bool | None = None
    is_cutoff: bool | None = None
    page_count: int | None = None
    confidence_avg: float | None = None
    comparison_fields: dict | None = None
    comparison_hash: str | None = None


class ExtractionRepository:
    """Async repository over the ``dmer_extraction`` table."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def upsert(self, record: ExtractionRecord) -> None:
        """Insert or update (on ``document_id``) the extraction row.

        Idempotent by design: a replayed extraction overwrites the existing row
        instead of creating a duplicate.
        """
        values: dict[str, object] = {
            "document_id": record.document_id,
            "licence_number_read": record.licence_number_read,
            "exam_date": record.exam_date,
            "physician_name": record.physician_name,
            "has_header": record.has_header,
            "has_signature": record.has_signature,
            "is_cutoff": record.is_cutoff,
            "page_count": record.page_count,
            "confidence_avg": record.confidence_avg,
            "comparison_fields": record.comparison_fields,
            "comparison_hash": record.comparison_hash,
        }
        update_cols = {k: v for k, v in values.items() if k != "document_id"}
        stmt = pg_insert(dmer_extraction).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[dmer_extraction.c.document_id], set_=update_cols
        )
        async with self._engine.begin() as conn:
            await conn.execute(stmt)
