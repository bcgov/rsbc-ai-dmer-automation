"""Unit tests for the pure/logic-heavy parts of function_app.py: the Mercury
URL builder and the per-record driver/dedup decisions in
_process_mercury_records. Everything that talks to Postgres, Blob Storage,
or the Mercury API itself is monkeypatched out -- nothing here needs
Azurite, a real Postgres, or network access (see ../local/mock_mercury_api.py
and ../integration/README.md for the tests that do).
"""

from __future__ import annotations

import json

import function_app as fa
import pytest


@pytest.fixture(autouse=True)
def mercury_env(monkeypatch):
    monkeypatch.setenv("MERCURY_API_BASE_URL", "https://mercury.example.com/documents")
    monkeypatch.setenv("MERCURY_QUEUE", "DPS_GENERAL")
    monkeypatch.setenv("MERCURY_PAGE_SIZE", "25")


def test_get_base_mercury_url_passes_queue_and_page_size_through():
    url = fa._get_base_mercury_url()
    assert url == "https://mercury.example.com/documents?queue=DPS_GENERAL&page_size=25"


def test_get_base_mercury_url_defaults_when_queue_and_page_size_unset(monkeypatch):
    monkeypatch.delenv("MERCURY_QUEUE", raising=False)
    monkeypatch.delenv("MERCURY_PAGE_SIZE", raising=False)
    url = fa._get_base_mercury_url()
    assert url == "https://mercury.example.com/documents?queue=DPS_GENERAL&page_size=50"


def test_process_mercury_records_uses_null_driver_license_when_no_driver(monkeypatch):
    """The core "not all DMERs have a driver attached" contract."""
    records = [
        {"document_guid": "A1", "document_url": "https://x/A1.pdf", "driver": None},
        {
            "document_guid": "A2",
            "document_url": "https://x/A2.pdf",
            "driver": {"licence_number": "123456"},
        },
    ]
    monkeypatch.setattr(fa, "_already_known_dmer_ids", lambda ids: set())
    monkeypatch.setattr(
        fa,
        "_download_to_blob",
        lambda url, dmer_id, container: (
            f"{container}/{dmer_id}.pdf",
            f"https://example.blob.core.windows.net/{container}/{dmer_id}.pdf",
        ),
    )
    monkeypatch.setattr(fa, "_publish_raw_dmer_message", lambda *a, **k: None)
    recorded = {}
    monkeypatch.setattr(
        fa,
        "_record_dmer_started",
        lambda dmer_id, driver_license, blob_path: recorded.setdefault(
            dmer_id, driver_license
        ),
    )

    new_count = fa._process_mercury_records(records, "raw")

    assert new_count == 2
    assert recorded["A1"] is None
    assert recorded["A2"] == "123456"


def test_process_mercury_records_skips_already_known_dmers(monkeypatch):
    records = [
        {"document_guid": "A1", "document_url": "https://x/A1.pdf", "driver": None}
    ]
    monkeypatch.setattr(fa, "_already_known_dmer_ids", lambda ids: {"A1"})
    download_calls = []
    monkeypatch.setattr(
        fa, "_download_to_blob", lambda *a, **k: download_calls.append(a) or "unused"
    )

    new_count = fa._process_mercury_records(records, "raw")

    assert new_count == 0
    assert download_calls == []


def test_process_mercury_records_records_failure_when_document_url_missing(monkeypatch):
    records = [{"document_guid": "A1", "document_url": None, "driver": None}]
    monkeypatch.setattr(fa, "_already_known_dmer_ids", lambda ids: set())
    failed = {}
    monkeypatch.setattr(
        fa,
        "_record_dmer_failed",
        lambda dmer_id, driver_license, error_message: failed.setdefault(
            dmer_id, error_message
        ),
    )

    new_count = fa._process_mercury_records(records, "raw")

    # Counted as "new" (attempted) even though it ultimately failed -- Mercury
    # still lists it, so the next poll will retry rather than skip it.
    assert new_count == 1
    assert "A1" in failed


def test_publish_raw_dmer_message_sends_expected_envelope(monkeypatch):
    """Verifies the raw-dmer-queue envelope (docs/contracts/queues/raw-dmer-queue.md)
    and that dmer_id is used as the Service Bus message_id (the duplicate-
    detection key), without needing a real Service Bus connection.
    """
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE_FQDN", "sb-test.servicebus.windows.net")
    sent = {}

    class FakeSender:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_messages(self, message):
            sent["message"] = message

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_queue_sender(self, queue_name):
            sent["queue_name"] = queue_name
            return FakeSender()

    class FakeMessage:
        def __init__(self, body, message_id):
            self.body = body
            self.message_id = message_id

    monkeypatch.setattr(fa, "ServiceBusClient", FakeClient)
    monkeypatch.setattr(fa, "ServiceBusMessage", FakeMessage)
    monkeypatch.setattr(fa, "DefaultAzureCredential", lambda: object())

    fa._publish_raw_dmer_message("DMER-1", "1234567", "https://x/DMER-1.pdf")

    assert sent["queue_name"] == fa._RAW_DMER_QUEUE_NAME
    assert sent["message"].message_id == "DMER-1"
    body = json.loads(sent["message"].body)
    assert body["messageId"] == "DMER-1"
    assert body["correlationId"] == "DMER-1"
    assert body["documentId"] == "DMER-1"
    assert body["documentUri"] == "https://x/DMER-1.pdf"
    assert body["mercuryCaseId"] is None
    assert body["sourceSystem"] == "mercury-batch"
    assert body["payload"]["driverLicense"] == "1234567"


def test_publish_raw_dmer_message_driver_license_null_in_payload(monkeypatch):
    monkeypatch.setenv("SERVICE_BUS_NAMESPACE_FQDN", "sb-test.servicebus.windows.net")
    sent = {}

    class FakeSender:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def send_messages(self, message):
            sent["message"] = message

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_queue_sender(self, queue_name):
            return FakeSender()

    class FakeMessage:
        def __init__(self, body, message_id):
            self.body = body
            self.message_id = message_id

    monkeypatch.setattr(fa, "ServiceBusClient", FakeClient)
    monkeypatch.setattr(fa, "ServiceBusMessage", FakeMessage)
    monkeypatch.setattr(fa, "DefaultAzureCredential", lambda: object())

    fa._publish_raw_dmer_message("DMER-2", None, "https://x/DMER-2.pdf")

    body = json.loads(sent["message"].body)
    assert body["payload"]["driverLicense"] is None
