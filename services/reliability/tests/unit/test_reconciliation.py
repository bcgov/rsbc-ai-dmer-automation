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
    """GET dmers: the documents still in the DPS queues, and whether more
    wait beyond them."""

    def __init__(self, records, has_more=False):
        self.records, self.has_more, self.calls = records, has_more, []

    def get_page(self, *, page_size):
        self.calls.append(page_size)
        return SimpleNamespace(records=self.records, has_more=self.has_more)


def _run(monkeypatch, mercury, entries, settings=None):
    seen = {}

    async def fake_reconcile(engine, documents, *, now, receive_grace, overdue):
        seen["documents"] = documents
        return entries

    async def fake_metrics(engine, *, now):
        return {"manual_review_24h": 2, "fallbacks_24h": 1, "stage_p95_seconds": {}}

    monkeypatch.setattr(rc, "reconcile", fake_reconcile)
    monkeypatch.setattr(rc, "daily_metrics", fake_metrics)
    saved = {}
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


def test_the_documents_still_in_the_dps_queues_are_checked_and_the_report_saved(
    monkeypatch,
):
    mercury = FakeMercury(
        [
            {"document_guid": "g1", "received_date": "2026-10-18T03:00:00-07:00"},
            {"document_guid": "g2"},
            {"document_name": "no guid"},
        ]
    )
    entry = ReportEntry(Finding.NOT_RECEIVED, "g2", None, None, None, None)
    report, saved, seen, mercury = _run(monkeypatch, mercury, [entry])
    # One GET dmers call: the next page only appears once these are marked,
    # which only the Page Poller does.
    assert mercury.calls == [2]
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


def test_more_waiting_than_one_page_is_reported_as_incomplete(monkeypatch):
    mercury = FakeMercury([{"document_guid": "g1"}], has_more=True)
    report, _, _, mercury = _run(monkeypatch, mercury, [])
    assert len(mercury.calls) == 1
    assert report["mercury_read_complete"] is False


def test_report_settings_from_app_settings(monkeypatch):
    monkeypatch.setenv("MERCURY_QUEUE", "DPS")
    monkeypatch.setenv("RECONCILIATION_OVERDUE_HOURS", "12")
    settings = fa.report_settings()
    assert settings.queue == "DPS"
    assert settings.overdue == timedelta(hours=12)
    assert settings.receive_grace == timedelta(hours=2)
