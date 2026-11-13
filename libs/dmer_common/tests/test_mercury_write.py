"""MercuryClient.send_outbox -- the Outbox Publisher's write-back (provisional, M-6)."""

import json

import pytest
from dmer_common.config import MercuryWriteSettings
from dmer_common.mercury_client import MercuryClient, MercuryWriteError
from dmer_common.retry import CircuitBreaker

WRITE = MercuryWriteSettings(base_url="https://mercury.test/api", api_key="SECRET-KEY")
GUID = "123e4567-e89b-12d3-a456-426614174000"


class FakeHttp:
    """Records calls; answers from a list of (status, body) or a callable."""

    def __init__(self, *responses):
        self.calls = []
        self._responses = list(responses)

    def __call__(self, method, url, *, headers, body):
        self.calls.append(
            (method, url, headers, json.loads(body) if body is not None else None)
        )
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        status, payload = response
        return status, json.dumps(payload).encode() if payload is not None else b""


def _client(http, breaker=None):
    return MercuryClient(
        write_settings=WRITE, http_send=http, breaker=breaker or CircuitBreaker()
    )


def test_update_outcome_puts_the_outcome_with_the_idempotency_key():
    http = FakeHttp((200, {"ok": True}))
    response = _client(http).send_outbox(
        "UPDATE_OUTCOME",
        {
            "document_guid": GUID,
            "outcome_code": "PU",
            "comment": "c",
            "decided_by": "AI",
        },
        "dec-1:UPDATE_OUTCOME",
    )
    assert response == {"ok": True}
    ((method, url, headers, body),) = http.calls
    assert (method, url) == (
        "PUT",
        f"https://mercury.test/api/documents/{GUID}/outcome",
    )
    assert headers["Idempotency-Key"] == "dec-1:UPDATE_OUTCOME"
    assert headers["Authorization"] == "Bearer SECRET-KEY"
    assert body == {"outcome_code": "PU", "comment": "c", "decided_by": "AI"}


def test_mark_duplicate_sets_rejected_and_detaches_from_the_case():
    http = FakeHttp((200, {}))
    _client(http).send_outbox(
        "MARK_DUPLICATE",
        {
            "document_guid": GUID,
            "status": "Rejected",
            "duplicate_of_document_guid": "other",
            "comment": "c",
        },
        "k",
    )
    ((method, url, _, body),) = http.calls
    assert (method, url) == ("PUT", f"https://mercury.test/api/documents/{GUID}/status")
    assert body["status"] == "Rejected" and body["detach_from_case"] is True


def test_create_case_attaches_to_the_drivers_open_case():
    http = FakeHttp((200, {"value": [{"case_id": "C1"}]}), (200, {"ok": True}))
    result = _client(http).send_outbox(
        "CREATE_CASE",
        {"document_guid": GUID, "mercury_driver_id": "D7", "comment": "c"},
        "k",
    )
    assert result["action"] == "attached" and result["case_id"] == "C1"
    get, put = http.calls
    assert (get[0], get[1]) == (
        "GET",
        "https://mercury.test/api/drivers/D7/cases?status=open",
    )
    assert "Idempotency-Key" not in get[2]  # a read
    assert (put[0], put[1]) == (
        "PUT",
        f"https://mercury.test/api/documents/{GUID}/case",
    )
    assert put[3] == {"case_id": "C1", "comment": "c"}


def test_create_case_creates_one_when_none_is_open():
    http = FakeHttp((200, {"value": []}), (201, {"case_id": "C9"}))
    result = _client(http).send_outbox(
        "CREATE_CASE",
        {"document_guid": GUID, "mercury_driver_id": "D7", "comment": "c"},
        "k",
    )
    assert result["action"] == "created"
    post = http.calls[1]
    assert (post[0], post[1]) == ("POST", "https://mercury.test/api/cases")
    assert post[3] == {"mercury_driver_id": "D7", "comment": "c", "document_guid": GUID}


def test_create_case_without_a_driver_id_is_a_permanent_failure():
    with pytest.raises(MercuryWriteError) as err:
        _client(FakeHttp()).send_outbox(
            "CREATE_CASE", {"document_guid": GUID, "mercury_driver_id": None}, "k"
        )
    assert err.value.retryable is False


@pytest.mark.parametrize(
    "status, retryable",
    [(500, True), (503, True), (429, True), (408, True), (400, False), (409, False)],
)
def test_failures_say_whether_a_retry_can_help(status, retryable):
    http = FakeHttp((status, {"error": "x"}))
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    assert (err.value.status, err.value.retryable) == (status, retryable)
    assert len(http.calls) == 1  # no in-call retry: the publisher backs off


def test_a_network_error_is_retryable():
    http = FakeHttp(OSError("connection reset"))
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    assert err.value.retryable is True and err.value.status is None


def test_an_open_breaker_is_retryable_and_calls_nothing():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=3600)
    http = FakeHttp((503, None))
    client = _client(http, breaker)
    with pytest.raises(MercuryWriteError):
        client.send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    with pytest.raises(MercuryWriteError) as err:
        client.send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    assert err.value.retryable is True
    assert len(http.calls) == 1


def test_a_rejected_write_does_not_trip_the_breaker():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=3600)
    http = FakeHttp((400, None), (200, {}))
    client = _client(http, breaker)
    with pytest.raises(MercuryWriteError):
        client.send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    client.send_outbox("UPDATE_OUTCOME", {"document_guid": GUID}, "k")
    assert len(http.calls) == 2


def test_unknown_operation_is_a_permanent_failure():
    with pytest.raises(MercuryWriteError) as err:
        _client(FakeHttp()).send_outbox(
            "DELETE_EVERYTHING", {"document_guid": GUID}, "k"
        )
    assert err.value.retryable is False


def test_api_key_and_comment_are_never_logged(caplog):
    http = FakeHttp((400, None))
    with pytest.raises(MercuryWriteError):
        _client(http).send_outbox(
            "UPDATE_OUTCOME",
            {"document_guid": GUID, "comment": "CLINICAL DETAIL"},
            "k",
        )
    assert "SECRET-KEY" not in caplog.text
    assert "CLINICAL DETAIL" not in caplog.text


def test_write_settings_default_from_config(monkeypatch):
    monkeypatch.setenv("MERCURY_WRITE_API_BASE_URL", "https://m.test/api/")
    monkeypatch.setenv("MERCURY_API_KEY", "k")
    assert MercuryClient().write_settings == MercuryWriteSettings(
        base_url="https://m.test/api", api_key="k"
    )
