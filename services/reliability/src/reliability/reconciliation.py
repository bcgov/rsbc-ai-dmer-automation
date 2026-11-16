"""Daily reconciliation report (09-reliability-components.md, azure-monitor.md).

Reads the documents still in Mercury's DPS queues (``GET dmers``), checks
every one against our tables (:func:`dmer_common.db.reconciliation.reconcile`), adds the
day's figures (:func:`daily_metrics`), saves the whole report as JSON to
``RECONCILIATION_CONTAINER`` (``<date>.json``) and logs a summary -- the line
the alert rules watch. The report holds ids, statuses and dates only.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from dmer_common.db.reconciliation import MercuryDocument, daily_metrics, reconcile
from dmer_common.telemetry import get_logger
from sqlalchemy.ext.asyncio import AsyncEngine

from ._runtime import run_sync

_log = get_logger(__name__)


@dataclass(frozen=True)
class ReportSettings:
    queue: str = "BOTH"
    page_size: int = 50
    receive_grace: timedelta = timedelta(hours=2)
    overdue: timedelta = timedelta(hours=24)


def _date(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


async def mercury_documents(
    client: Any, settings: ReportSettings
) -> tuple[list[MercuryDocument], bool]:
    """The documents still in the DPS queues -- the ones the Page Poller has
    not yet marked AI Processed -- and whether that was all of them.

    One page only: ``GET dmers`` is claim-based, so the next page appears
    only once these are marked, and marking is the Page Poller's job, not
    the report's. A document still here past ``receive_grace`` means the
    poller is not keeping up (or not running)."""
    documents: dict[str, MercuryDocument] = {}
    page = await run_sync(client.get_page, page_size=settings.page_size)
    for record in page.records:
        guid = str(record.get("document_guid") or "")
        if guid:
            documents[guid] = MercuryDocument(guid, _date(record.get("received_date")))
    return list(documents.values()), not page.has_more


async def run_report(
    engine: AsyncEngine,
    client: Any,
    save: Callable[[str, bytes], None],
    *,
    settings: ReportSettings,
    clock: Callable[[], datetime],
) -> dict[str, Any]:
    """One report: build, save (``save(blob_name, body)``), log, return it."""
    now = clock()
    documents, complete = await mercury_documents(client, settings)
    entries = await reconcile(
        engine,
        documents,
        now=now,
        receive_grace=settings.receive_grace,
        overdue=settings.overdue,
    )
    findings = Counter(entry.finding.value for entry in entries)
    report = {
        "generated_at": now.isoformat(),
        "mercury_queue": settings.queue,
        "mercury_documents": len(documents),
        "mercury_read_complete": complete,
        "findings": dict(findings),
        "metrics": await daily_metrics(engine, now=now),
        "entries": [entry.as_dict() for entry in entries],
    }
    name = f"{now:%Y-%m-%d}.json"
    await run_sync(save, name, json.dumps(report, indent=1).encode("utf-8"))
    log = _log.error if entries or not complete else _log.info
    log(
        "daily reconciliation report",
        extra={
            "report_blob": name,
            "mercury_documents": len(documents),
            "mercury_read_complete": complete,
            "unaccounted": len(entries),
            **{f"finding_{k.lower()}": v for k, v in findings.items()},
            **{k: v for k, v in report["metrics"].items() if not isinstance(v, dict)},
            "stage_p95_seconds": report["metrics"]["stage_p95_seconds"],
        },
    )
    return report
