"""Daily reconciliation report (09-reliability-components.md, azure-monitor.md).

Pages through Mercury's DPS queue (the batch API), checks every document
against our tables (:func:`dmer_common.db.reconciliation.reconcile`), adds the
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
    max_pages: int = 2000  # bounds one run (100k documents at 50 a page)
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
    """Every document in the DPS queue (up to ``max_pages``); and whether the
    queue was read to the end."""
    documents: dict[str, MercuryDocument] = {}
    next_url = None
    for _ in range(settings.max_pages):
        page = await run_sync(
            client.get_page,
            queue=settings.queue,
            page_size=settings.page_size,
            next_url=next_url,
        )
        for record in page.records:
            guid = str(record.get("document_guid") or "")
            if guid:
                documents[guid] = MercuryDocument(
                    guid, _date(record.get("received_date"))
                )
        next_url = page.next_url
        if not next_url:
            return list(documents.values()), True
    return list(documents.values()), False


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
