"""Unit tests for the Mercury client (faked HTTP).

Behaviour specs (GIVEN/WHEN/THEN) for GET dmers (Mercury's camelCase items
translated to the pipeline's records, receivedDate read in a time zone), the
AI Processed status call, retry on transient failure, non-2xx handling, and
-- critically -- that the API key is never logged.
"""

from __future__ import annotations

import io
import json
import logging
from urllib.parse import urlparse

import pytest
from dmer_common import mercury_client as client_module
from dmer_common.config import MercurySettings
from dmer_common.mercury_client import MercuryApiError, MercuryClient

SECRET_KEY = "super-secret-mercury-key"  # pragma: allowlist secret

SETTINGS = MercurySettings(
    base_url="https://mercury.example.com/Dmers",
    api_key=SECRET_KEY,
)

DMER_ITEM = {
    "documentRecordId": "5411035e-af76-ef11-b84f-005056830319",
    "queue": "Team - Adjudicators",
    "documentName": "DMER-99934205 - TEST.pdf",
    "documentUrl": "dfp_driver/triage-request/DMER-00200008.pdf",
    "documentType": "DMER",
    "submittalStatus": "Uploaded",
    "documentPriority": "",
    "receivedDate": "05/06/2025 10:03:01",
    "documentTypeBusinessArea": "Driver Fitness",
    "dmerStatus": "Uploaded",
    "driver": {
        "driverId": "25100b61-76ce-ed11-b841-00505683fbf4",
        "firstName": "",
        "middleName": "",
        "lastName": "",
        "licenceNumber": "99934205",
        "documents": [],
    },
    "dmerDocumentCaseRecord": {
        "caseId": "28100b61-76ce-ed11-b841-00505683fbf4",
        "caseTitle": "99934205- TEST- DMER- 1",
        "caseType": "Suspected Medical Condition",
        "casePriority": "High",
        "caseOwner": "Team - Intake",
        "caseStatus": "Unknown",
    },
}
HRDIR_ITEM = {
    "documentRecordId": "05382879-02d0-ed11-b841-00505683fbf4",
    "documentUrl": "",
    "documentType": "HRDIR High Risk Driving Incident Report",
    "receivedDate": "03/31/2023 20:27:44",
    "driver": None,
    "dmerDocumentCaseRecord": None,
}
PAGE = {
    "items": [DMER_ITEM, HRDIR_ITEM],
    "page_size": 2,
    "nextLink": "/Dmers?pageSize=2",
}
LAST_PAGE = {"items": [], "page_size": 2, "nextLink": None}


class _FakeHttp:
    def __init__(self, page: dict = PAGE, *, fail_times: int = 0) -> None:
        self.page = page
        self.fail_times = fail_times
        self.calls: list[dict] = []

    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        self.calls.append({"url": url, "headers": headers})
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("transient network error")
        return 200, json.dumps(self.page).encode("utf-8")


class _FakeSend:
    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.calls: list[dict] = []

    def __call__(self, method, url, *, headers, body):
        self.calls.append(
            {"method": method, "url": url, "headers": headers, "body": body}
        )
        return self.status, b'{"ok": true}'


def test_get_page_calls_get_dmers_with_the_page_size():
    http = _FakeHttp()
    client = MercuryClient(settings=SETTINGS, http_get=http)
    page = client.get_page(page_size=50)
    assert http.calls[0]["url"] == "https://mercury.example.com/Dmers?pageSize=50"
    assert [r["document_guid"] for r in page.records] == [
        DMER_ITEM["documentRecordId"],
        HRDIR_ITEM["documentRecordId"],
    ]
    assert page.has_more is True


def test_get_page_translates_mercury_items_to_pipeline_records():
    # GIVEN Mercury's camelCase item WHEN fetched THEN the pipeline's
    # snake_case record, with "" read as unset
    client = MercuryClient(settings=SETTINGS, http_get=_FakeHttp())
    record = client.get_page().records[0]
    assert record == {
        "document_guid": "5411035e-af76-ef11-b84f-005056830319",
        "document_name": "DMER-99934205 - TEST.pdf",
        "document_url": "dfp_driver/triage-request/DMER-00200008.pdf",
        "document_type": "DMER",
        "document_status": "Uploaded",
        "dmer_status": "Uploaded",
        "document_priority": None,
        "received_date": "2025-05-06T10:03:01-07:00",
        "document_type_business_area": "Driver Fitness",
        "queue": "Team - Adjudicators",
        "driver": {
            "driver_id": "25100b61-76ce-ed11-b841-00505683fbf4",
            "first_name": None,
            "middle_name": None,
            "last_name": None,
            "licence_number": "99934205",
            "documents": [],
        },
        "case": {
            "case_id": "28100b61-76ce-ed11-b841-00505683fbf4",
            "case_title": "99934205- TEST- DMER- 1",
            "case_type": "Suspected Medical Condition",
            "case_priority": "High",
            "case_owner": "Team - Intake",
            "case_status": "Unknown",
        },
    }


def test_non_dmer_items_are_returned_too_without_driver_or_case():
    client = MercuryClient(settings=SETTINGS, http_get=_FakeHttp())
    record = client.get_page().records[1]
    assert record["document_type"] == "HRDIR High Risk Driving Incident Report"
    assert record["document_url"] is None
    assert record["driver"] is None and record["case"] is None
    # March 31 is daylight time in Pacific (PDT, -07:00)
    assert record["received_date"] == "2023-03-31T20:27:44-07:00"


@pytest.mark.parametrize(
    ("received", "expected"),
    [
        ("2025-05-06T17:03:01Z", "2025-05-06T17:03:01+00:00"),  # ISO-8601 UTC kept
        ("2025-05-06T10:03:01", "2025-05-06T10:03:01-07:00"),  # ISO, no offset
        ("01/15/2025 08:00:00", "2025-01-15T08:00:00-08:00"),  # standard time
        ("", None),
        ("not a date", None),
    ],
)
def test_received_date_forms(received, expected):
    page = {"items": [{**DMER_ITEM, "receivedDate": received}], "nextLink": None}
    client = MercuryClient(settings=SETTINGS, http_get=_FakeHttp(page))
    assert client.get_page().records[0]["received_date"] == expected


def test_received_date_time_zone_is_configurable():
    settings = MercurySettings(
        base_url=SETTINGS.base_url, api_key=SECRET_KEY, received_date_timezone="UTC"
    )
    client = MercuryClient(settings=settings, http_get=_FakeHttp())
    assert client.get_page().records[0]["received_date"] == "2025-05-06T10:03:01+00:00"


def test_no_next_link_means_nothing_more_waits():
    page = MercuryClient(settings=SETTINGS, http_get=_FakeHttp(LAST_PAGE)).get_page()
    assert page.records == [] and page.has_more is False


def test_get_page_sends_bearer_auth_header():
    http = _FakeHttp()
    MercuryClient(settings=SETTINGS, http_get=http).get_page()
    assert http.calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"


def test_get_page_retries_transient_failure():
    # GIVEN a client whose first call fails then succeeds
    http = _FakeHttp(fail_times=1)
    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching a page THEN the retry policy recovers
    assert len(client.get_page().records) == 2
    assert len(http.calls) == 2


def test_non_2xx_response_raises_mercury_api_error():
    # GIVEN an HTTP GET that returns a 500
    def http(url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        return 500, b'{"error": "boom"}'

    client = MercuryClient(settings=SETTINGS, http_get=http)
    # WHEN fetching a page THEN a MercuryApiError is raised (after retries exhaust)
    with pytest.raises(MercuryApiError):
        client.get_page()


def test_api_key_is_never_logged():
    # GIVEN the module logger writing to an in-memory buffer
    buffer = io.StringIO()
    handler = client_module._log.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)

    client = MercuryClient(settings=SETTINGS, http_get=_FakeHttp())
    # WHEN a page is fetched and logged
    client.get_page()
    # THEN the emitted JSON never contains the API key
    logged = buffer.getvalue()
    assert SECRET_KEY not in logged
    record = json.loads(logged.strip().splitlines()[-1])
    assert record["record_count"] == 2
    assert record["has_next"] is True


def test_settings_default_from_config(monkeypatch):
    # GIVEN Mercury config in the environment and no explicit settings
    monkeypatch.setenv("MERCURY_API_BASE_URL", "https://m.example.com/Dmers/")
    monkeypatch.setenv("MERCURY_API_KEY", "k")
    monkeypatch.delenv("MERCURY_RECEIVED_DATE_TIMEZONE", raising=False)
    http = _FakeHttp()
    # WHEN constructing without settings THEN it loads from config
    page = MercuryClient(http_get=http).get_page()
    assert len(page.records) == 2
    assert urlparse(http.calls[0]["url"]).hostname == "m.example.com"
    assert http.calls[0]["url"] == "https://m.example.com/Dmers?pageSize=50"
    assert page.records[0]["received_date"].endswith("-07:00")  # Pacific default


# ---------------------------------------------------------------------------
# AI Processed status
# ---------------------------------------------------------------------------

GUIDS = [
    "5411035e-af76-ef11-b84f-005056830319",
    "05382879-02d0-ed11-b841-00505683fbf4",
]


def test_set_ai_processed_posts_the_ids():
    send = _FakeSend()
    MercuryClient(settings=SETTINGS, http_send=send).set_ai_processed(GUIDS)
    call = send.calls[0]
    assert call["method"] == "POST"
    assert (
        call["url"]
        == "https://mercury.example.com/Dmers/BatchSetDmerAIProccessedStatus"
    )
    assert json.loads(call["body"]) == {"dmerIds": GUIDS}
    assert call["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"
    assert call["headers"]["Content-Type"] == "application/json"


def test_set_ai_processed_with_nothing_to_mark_calls_nothing():
    send = _FakeSend()
    MercuryClient(settings=SETTINGS, http_send=send).set_ai_processed([])
    assert send.calls == []


def test_set_ai_processed_retries_then_raises_on_failure():
    send = _FakeSend(status=503)
    with pytest.raises(MercuryApiError):
        MercuryClient(settings=SETTINGS, http_send=send).set_ai_processed(GUIDS)
    assert len(send.calls) == 4  # mercury_retry's attempts


def test_set_ai_processed_logs_a_count_not_the_ids_or_key(caplog):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger(client_module.__name__).addHandler(handler)
    try:
        client = MercuryClient(settings=SETTINGS, http_send=_FakeSend())
        client.set_ai_processed(GUIDS)
    finally:
        logging.getLogger(client_module.__name__).removeHandler(handler)
    logged = stream.getvalue() + caplog.text
    assert "mercury AI processed status set" in logged
    assert all(guid not in logged for guid in GUIDS)
    assert SECRET_KEY not in logged


# ---------------------------------------------------------------------------
# GET by driver_licence
# ---------------------------------------------------------------------------

from dmer_common.config import MercuryDriverSettings

LICENCE = "01234567"
DRIVER_SETTINGS = MercuryDriverSettings(
    base_url="https://mercury.example.com/api/mercury/drivers",
    api_key=SECRET_KEY,
    counted_document_types=frozenset({"dmer"}),
    uncounted_document_statuses=frozenset({"rejected"}),
)
DRIVER = {"driver_id": "D1234", "licence_number": "1234567", "active_documents": []}


def _driver_client(status: int, body: object = None, *, fail_times: int = 0):
    calls: list[dict] = []

    def http_get(url, *, headers):
        calls.append({"url": url, "headers": headers})
        if len(calls) <= fail_times:
            raise ConnectionError("transient network error")
        return status, json.dumps(body).encode("utf-8")

    return MercuryClient(driver_settings=DRIVER_SETTINGS, http_get=http_get), calls


def test_driver_lookup_appends_the_licence_and_sends_the_bearer_key():
    client, calls = _driver_client(200, DRIVER)
    assert client.get_driver_by_licence(LICENCE) == [DRIVER]
    assert (
        calls[0]["url"] == f"https://mercury.example.com/api/mercury/drivers/{LICENCE}"
    )
    assert calls[0]["headers"]["Authorization"] == f"Bearer {SECRET_KEY}"


def test_driver_lookup_not_found_is_an_empty_list():
    client, _ = _driver_client(404, {"error": "driver not found"})
    assert client.get_driver_by_licence(LICENCE) == []


def test_driver_lookup_returning_several_drivers_returns_them_all():
    client, _ = _driver_client(200, [DRIVER, {**DRIVER, "driver_id": "D9"}])
    assert len(client.get_driver_by_licence(LICENCE)) == 2


def test_driver_lookup_retries_transient_failures():
    client, calls = _driver_client(200, DRIVER, fail_times=1)
    assert client.get_driver_by_licence(LICENCE) == [DRIVER]
    assert len(calls) == 2


def test_driver_lookup_server_error_raises():
    client, _ = _driver_client(500, {"error": "boom"})
    with pytest.raises(MercuryApiError):
        client.get_driver_by_licence(LICENCE)


def test_driver_lookup_never_logs_the_licence_or_key(caplog):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logging.getLogger(client_module.__name__).addHandler(handler)
    try:
        for status in (200, 500):
            client, _ = _driver_client(status, DRIVER)
            try:
                client.get_driver_by_licence(LICENCE)
            except MercuryApiError:
                pass
    finally:
        logging.getLogger(client_module.__name__).removeHandler(handler)
    logged = stream.getvalue() + caplog.text
    assert LICENCE not in logged and "1234567" not in logged
    assert SECRET_KEY not in logged


def test_batch_only_service_needs_no_driver_settings(monkeypatch):
    # The orchestrator has no batch URL and the poller no driver URL; neither
    # is read until that API is used.
    monkeypatch.delenv("MERCURY_DRIVER_LICENCE_API_BASE_URL", raising=False)
    monkeypatch.delenv("MERCURY_API_BASE_URL", raising=False)
    MercuryClient(http_get=lambda url, *, headers: (200, b"{}"))


def test_driver_settings_from_environment(monkeypatch):
    from dmer_common.config import mercury_driver_settings

    monkeypatch.setenv(
        "MERCURY_DRIVER_LICENCE_API_BASE_URL", "https://m.example.com/drivers/"
    )
    monkeypatch.setenv("MERCURY_API_KEY", SECRET_KEY)
    monkeypatch.setenv("MERCURY_COUNTED_DOCUMENT_TYPES", "DMER, Test Report")
    monkeypatch.delenv("MERCURY_UNCOUNTED_DOCUMENT_STATUSES", raising=False)
    settings = mercury_driver_settings()
    assert settings.base_url == "https://m.example.com/drivers"
    assert settings.counted_document_types == {"dmer", "test report"}
    assert settings.uncounted_document_statuses == {"rejected"}
