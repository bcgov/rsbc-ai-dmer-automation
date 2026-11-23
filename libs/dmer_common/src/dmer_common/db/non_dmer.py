"""Non-DMER records on a Mercury page: sent back to Mercury as an error.

``GET dmers`` also returns other document types (HRDIR reports, backfiles,
...). The Page Poller claims every record on the page -- otherwise they would
come back on every poll and fill the page -- but only DMERs are processed.
Each non-DMER gets an outcome instead (decided 2026-11-23): Mercury's
``SetDmerAIProccessedStatus`` with ``aiProcessedStatus`` Error, routed to
Team - Intake, notes "Unable to AI process due to error. The document is not
a DMER (document type: ...)." -- so Intake sees it instead of it sitting
InProgress in the AI Processed Queue.

In one transaction (:func:`record_non_dmer`):

- ``dmer_document``: a row for the record, straight to ``MANUAL_REVIEW``
  (``current_stage`` INGEST, no driver), so the outbox has a document to hang
  off. ``ON CONFLICT (document_guid) DO NOTHING``;
- ``processing_error``: ``INGEST`` / ``PERMANENT_BUSINESS`` / ``NOT_A_DMER``;
- ``mercury_outbox``: the fallback ``UPDATE_OUTCOME`` (IN, ``decided_by``
  FALLBACK), which the Outbox Publisher sends as a processing error. Key
  ``<document_id>:FALLBACK:UPDATE_OUTCOME``, so a re-poll can't queue twice.

The outbox row is due ``send_after`` from now, not immediately: it is queued
before the page is claimed, and the outcome must reach Mercury after the claim
(which sets InProgress), never before it.

A ``document_guid`` already in ``dmer_document`` as anything but
``MANUAL_REVIEW`` (e.g. it was a DMER) is left alone: nothing is queued.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from ..telemetry import get_logger
from .dmer_document import dmer_document
from .mercury_outbox import fallback_comment, fallback_rows, queue_outbox_rows
from .processing_error import FailureCategory, record_processing_error

_log = get_logger(__name__)

NOT_A_DMER: Final = "NOT_A_DMER"
# How long after queuing the outcome may be sent -- comfortably after the
# claim, which follows within the same poll.
DEFAULT_SEND_AFTER: Final = timedelta(minutes=5)


def not_a_dmer_comment(document_type: str | None) -> str:
    """The note Mercury gets for a non-DMER record."""
    kind = (document_type or "").strip() or "unknown"
    return fallback_comment(f"The document is not a DMER (document type: {kind}).")


@dataclass(frozen=True)
class NonDmerResult:
    document_id: str
    queued: bool  # False: already queued, or the guid belongs to a DMER


async def record_non_dmer(
    engine: AsyncEngine,
    record: dict[str, Any],
    *,
    now: datetime,
    send_after: timedelta = DEFAULT_SEND_AFTER,
) -> NonDmerResult | None:
    """Store a non-DMER *record* (snake_case, from ``MercuryClient``) and queue
    its error outcome for Mercury. None when the record has no
    ``document_guid``."""
    document_guid = record.get("document_guid")
    if not document_guid:
        return None
    case = record.get("case") or {}
    comment = not_a_dmer_comment(record.get("document_type"))
    async with engine.begin() as conn:
        inserted = (
            await conn.execute(
                pg_insert(dmer_document)
                .values(
                    document_guid=document_guid,
                    document_name=record.get("document_name"),
                    mercury_document_status=record.get("document_status"),
                    document_priority=record.get("document_priority"),
                    received_date=record.get("received_date"),
                    queue=record.get("queue"),
                    business_area=record.get("document_type_business_area"),
                    mercury_case_id=case.get("case_id") if case else None,
                    document_url=record.get("document_url"),
                    pipeline_status="MANUAL_REVIEW",
                    current_stage="INGEST",
                    attempt_count=0,
                    first_seen_at=now,
                    updated_at=now,
                )
                .on_conflict_do_nothing(index_elements=[dmer_document.c.document_guid])
                .returning(dmer_document.c.id)
            )
        ).first()
        if inserted is not None:
            document_id = str(inserted.id)
            await record_processing_error(
                conn,
                document_id=document_id,
                stage="INGEST",
                category=FailureCategory.PERMANENT_BUSINESS,
                reason_code=NOT_A_DMER,
                message="Not a DMER: not processed by the AI.",
                now=now,
            )
        else:
            existing = (
                await conn.execute(
                    select(dmer_document.c.id, dmer_document.c.pipeline_status).where(
                        dmer_document.c.document_guid == document_guid
                    )
                )
            ).one()
            document_id = str(existing.id)
            if existing.pipeline_status != "MANUAL_REVIEW":
                _log.warning(
                    "non-DMER record matches a document in the pipeline; not queued",
                    extra={"document_id": document_id},
                )
                return NonDmerResult(document_id, queued=False)
        queued = await queue_outbox_rows(
            conn,
            fallback_rows(
                source_id=f"{document_id}:FALLBACK",
                document_id=document_id,
                document_guid=str(document_guid),
                comment=comment,
            ),
            now=now + send_after,
        )
    return NonDmerResult(document_id, queued=bool(queued))
