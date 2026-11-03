"""Daily reconciliation report: is every DMER Mercury is waiting on accounted for?

See docs/development/stages/09-reliability-components.md ("Daily reconciliation
report") and services/azure-monitor.md. The caller pages through Mercury's
DPS queue (the batch API: documents not yet triaged); :func:`reconcile`
checks each one against our tables and returns what needs a person:

- ``NOT_RECEIVED`` -- in Mercury for longer than ``receive_grace`` with no
  ``dmer_document`` row: a webhook or poll page was missed;
- ``OVERDUE`` -- still in the pipeline (not decided, not in manual review)
  ``overdue`` after we first saw it;
- ``MANUAL_REVIEW_NO_OUTCOME`` -- in ``MANUAL_REVIEW`` with nothing queued to
  Mercury: Intake will never see it in the AI-processed queue;
- ``NOT_POSTED`` -- an outcome is queued but not every write is ``SENT``
  (``PENDING`` past ``overdue``);
- ``POST_FAILED`` -- a write is ``FAILED``: a person resolves it in Mercury.

A document whose outbox rows are all ``SENT`` is accounted for. Only ids,
statuses and dates go in the report -- no licences, no clinical content.

:func:`daily_metrics` adds the day's figures the alerts watch: documents
routed to ``MANUAL_REVIEW``, fallback outcomes queued, and stage-duration
p95s from ``dmer_stage_run``.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_CHUNK: Final = 500


class Finding(str, enum.Enum):
    NOT_RECEIVED = "NOT_RECEIVED"
    OVERDUE = "OVERDUE"
    MANUAL_REVIEW_NO_OUTCOME = "MANUAL_REVIEW_NO_OUTCOME"
    NOT_POSTED = "NOT_POSTED"
    POST_FAILED = "POST_FAILED"


@dataclass(frozen=True)
class MercuryDocument:
    """One document in Mercury's DPS queue."""

    document_guid: str
    received_date: datetime | None


@dataclass(frozen=True)
class ReportEntry:
    finding: Finding
    document_guid: str
    document_id: str | None
    pipeline_status: str | None
    received_date: datetime | None
    first_seen_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "finding": self.finding.value,
            "document_guid": self.document_guid,
            "document_id": self.document_id,
            "pipeline_status": self.pipeline_status,
            "received_date": _iso(self.received_date),
            "first_seen_at": _iso(self.first_seen_at),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


_DOCUMENTS = text("""
    SELECT d.document_guid::text AS guid,
           d.id::text AS document_id,
           d.pipeline_status::text AS status,
           d.first_seen_at,
           count(o.id) AS writes,
           count(o.id) FILTER (WHERE o.status = 'SENT') AS sent,
           count(o.id) FILTER (WHERE o.status = 'FAILED') AS failed
    FROM dmer_document d
    LEFT JOIN mercury_outbox o ON o.document_id = d.id
    WHERE d.document_guid = ANY(CAST(:guids AS uuid[]))
    GROUP BY d.id
    """)

# Statuses where the document is still moving through the pipeline.
_IN_PIPELINE: Final = frozenset(
    {
        "RECEIVED",
        "DOWNLOADED",
        "EXTRACTING",
        "EXTRACTED",
        "NORMALIZED",
        "RULES_APPLIED",
        "AWAITING_DRIVER_COMPLETION",
    }
)


def _finding(row, *, now: datetime, overdue: timedelta) -> Finding | None:
    late = row.first_seen_at is not None and row.first_seen_at < now - overdue
    if row.failed:
        return Finding.POST_FAILED
    if row.writes:
        return (
            None if row.sent == row.writes else (Finding.NOT_POSTED if late else None)
        )
    if row.status == "MANUAL_REVIEW":
        return Finding.MANUAL_REVIEW_NO_OUTCOME
    if row.status in _IN_PIPELINE or row.status in ("DECIDED", "POSTING"):
        return Finding.OVERDUE if late else None
    return None  # COMPLETED with no writes can't happen; nothing to say


async def reconcile(
    engine: AsyncEngine,
    documents: Iterable[MercuryDocument],
    *,
    now: datetime,
    receive_grace: timedelta,
    overdue: timedelta,
) -> list[ReportEntry]:
    """The documents in Mercury's DPS queue that aren't accounted for."""
    documents = list(documents)
    entries: list[ReportEntry] = []
    async with engine.connect() as conn:
        for start in range(0, len(documents), _CHUNK):
            chunk = documents[start : start + _CHUNK]
            rows = {
                row.guid: row
                for row in (
                    await conn.execute(
                        _DOCUMENTS, {"guids": [d.document_guid for d in chunk]}
                    )
                ).all()
            }
            for document in chunk:
                row = rows.get(document.document_guid)
                if row is None:
                    received = document.received_date
                    if received is None or received < now - receive_grace:
                        entries.append(
                            ReportEntry(
                                Finding.NOT_RECEIVED,
                                document.document_guid,
                                None,
                                None,
                                received,
                                None,
                            )
                        )
                    continue
                finding = _finding(row, now=now, overdue=overdue)
                if finding is not None:
                    entries.append(
                        ReportEntry(
                            finding,
                            document.document_guid,
                            row.document_id,
                            row.status,
                            document.received_date,
                            row.first_seen_at,
                        )
                    )
    return entries


async def daily_metrics(engine: AsyncEngine, *, now: datetime) -> dict[str, Any]:
    """The last 24 hours: documents routed to MANUAL_REVIEW, fallback outcomes
    queued, and p95 stage durations in seconds (succeeded runs)."""
    since = now - timedelta(days=1)
    async with engine.connect() as conn:
        manual_review = (
            await conn.execute(
                text(
                    "SELECT count(*) FROM dmer_document "
                    "WHERE pipeline_status = 'MANUAL_REVIEW' AND updated_at >= :since"
                ),
                {"since": since},
            )
        ).scalar_one()
        fallbacks = (
            await conn.execute(
                text(
                    "SELECT count(DISTINCT o.document_id) FROM mercury_outbox o "
                    "JOIN dmer_document d ON d.id = o.document_id "
                    "WHERE o.payload->>'decided_by' = 'FALLBACK' "
                    "AND d.updated_at >= :since"
                ),
                {"since": since},
            )
        ).scalar_one()
        durations = (
            await conn.execute(
                text(
                    "SELECT stage::text, count(*) AS runs, percentile_cont(0.95) "
                    "WITHIN GROUP (ORDER BY extract(epoch FROM ended_at - started_at)) "
                    "AS p95 FROM dmer_stage_run WHERE status = 'SUCCEEDED' "
                    "AND ended_at >= :since GROUP BY stage"
                ),
                {"since": since},
            )
        ).all()
    return {
        "manual_review_24h": manual_review,
        "fallbacks_24h": fallbacks,
        "stage_p95_seconds": {row.stage: round(row.p95, 1) for row in durations},
        "stage_runs_24h": {row.stage: row.runs for row in durations},
    }
