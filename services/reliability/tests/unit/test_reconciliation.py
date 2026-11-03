"""The daily reconciliation report's Mercury paging, saving and summary (the
checks themselves are tested in dmer_common, against PostgreSQL)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import reliability.function_app as fa
import reliability.reconciliation as rc
from dmer_common.db.reconciliation import Finding, MercuryDocument, ReportEntry

NOW = datetime(2026, 10, 19, 13, tzinfo=UTC)


class FakeMercury:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get_page(self, *, queue, page_size, next_url=None):
        self.calls.append((queue, page_size, next_url))
        records, next_link = self.pages[len(self.calls) - 1]
        return SimpleNamespace(records=records, next_url=next_link)


def _run(monkeypatch, pages, entries, settings=None):
    seen = {}

    async def fake_reconcile(engine, documents, *, now, receive_grace, overdue):
        seen["documents"] = documents
        return entries

    async def fake_metrics(engine, *, now):
        return {"manual_review_24h": 2, "fallbacks_24h": 1, "stage_p95_seconds": {}}

    monkeypatch.setattr(rc, "reconcile", fake_reconcile)
    monkeypatch.setattr(rc, "daily_metrics", fake_metrics)
    saved = {}
    mercury = FakeMercury(pages)
    report = asyncio.run(
        rc.run_report(
            object(),
            mercury,
            lambda name, body: saved.update({name: json.loads(body)}),
            settings=settings or rc.ReportSettings(page_size=2),
            clock=lambda: NOW,
        )
    )
    return report, saved, seen, mercury


def test_every_page_of_the_dps_queue_is_checked_and_the_report_saved(monkeypatch):
    pages = [
        ([{"document_guid": "g1", "received_date": "2026-10-18T10:00:00Z"}], "next"),
        ([{"document_guid": "g2"}, {"document_name": "no guid"}], None),
    ]
    entry = ReportEntry(Finding.NOT_RECEIVED, "g2", None, None, None, None)
    report, saved, seen, mercury = _run(monkeypatch, pages, [entry])
    assert [c[2] for c in mercury.calls] == [None, "next"]
    assert seen["documents"] == [
        MercuryDocument("g1", datetime(2026, 10, 18, 10, tzinfo=UTC)),
        MercuryDocument("g2", None),
    ]
    assert list(saved) == ["2026-10-19.json"]
    assert saved["2026-10-19.json"] == report
    assert report["mercury_documents"] == 2
    assert report["mercury_read_complete"] is True
    assert report["findings"] == {"NOT_RECEIVED": 1}
    assert report["entries"][0]["document_guid"] == "g2"
    assert report["metrics"]["manual_review_24h"] == 2


def test_a_queue_longer_than_max_pages_is_reported_as_incomplete(monkeypatch):
    pages = [([{"document_guid": f"g{i}"}], f"p{i + 1}") for i in range(5)]
    report, _, _, mercury = _run(
        monkeypatch, pages, [], settings=rc.ReportSettings(max_pages=3)
    )
    assert len(mercury.calls) == 3
    assert report["mercury_read_complete"] is False


def test_report_settings_from_app_settings(monkeypatch):
    monkeypatch.setenv("MERCURY_QUEUE", "DPS")
    monkeypatch.setenv("RECONCILIATION_OVERDUE_HOURS", "12")
    settings = fa.report_settings()
    assert settings.queue == "DPS"
    assert settings.overdue == timedelta(hours=12)
    assert settings.receive_grace == timedelta(hours=2)
