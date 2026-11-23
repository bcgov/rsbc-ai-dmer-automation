"""MercuryClient.send_outbox -- the Outbox Publisher's write-back."""

import json

import pytest
from dmer_common import mercury_client as client_module
from dmer_common.config import MercuryWriteSettings
from dmer_common.mercury_client import MercuryClient, MercuryWriteError
from dmer_common.retry import CircuitBreaker

WRITE = MercuryWriteSettings(base_url="https://mercury.test/api", api_key="SECRET-KEY")
GUID = "123e4567-e89b-12d3-a456-426614174000"


class _FakeAuth:
    """A bearer token without the token endpoint (tested in test_mercury_auth)."""

    token_value = "test-token"

    def token(self):
        return self.token_value

    def invalidate(self, token):
        pass


@pytest.fixture(autouse=True)
def _fake_token(monkeypatch):
    monkeypatch.setattr(
        client_module, "shared_auth", lambda settings, *, http_get: _FakeAuth()
    )
    monkeypatch.setattr(client_module, "mercury_auth_settings", lambda: None)


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


AI_STATUS_URL = "https://mercury.test/api/dmers/SetDmerAIProccessedStatus"
PAYLOAD = {
    "document_guid": GUID,
    "outcome_code": "IN",
    "comment": "c",
    "decided_by": "AI",
}

# Mercury's option-set values (SetDmerAIProccessedStatus).
PROCESSED, ERROR = 100000002, 100000003
ADJUDICATE, MANUAL_PASS = 100000000, 100000003
INTAKE, ADJUDICATORS, CASE_MANAGERS = 100000000, 100000001, 100000002
REGULAR, URGENT, CRITICAL = 100000000, 100000001, 100000003


def _sent_body(operation, payload, key="k"):
    http = FakeHttp((200, {"ok": True}))
    _client(http).send_outbox(operation, payload, key)
    ((method, url, _, body),) = http.calls
    assert (method, url) == ("POST", AI_STATUS_URL)
    return body


def test_update_outcome_posts_the_ai_status_with_the_idempotency_key():
    http = FakeHttp((200, {"ok": True}))
    response = _client(http).send_outbox(
        "UPDATE_OUTCOME",
        {**PAYLOAD, "outcome_code": "PU", "comment": "AI outcome: PU. Reason: r."},
        "dec-1:UPDATE_OUTCOME",
    )
    assert response == {"ok": True}
    ((method, url, headers, body),) = http.calls
    assert (method, url) == ("POST", AI_STATUS_URL)
    assert headers["Idempotency-Key"] == "dec-1:UPDATE_OUTCOME"
    assert headers["Authorization"] == "Bearer test-token"
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": PROCESSED,
        "aiDpsPriority": URGENT,
        "aiQueue": ADJUDICATORS,
        "aiDmerStatus": ADJUDICATE,
        "notes": "AI outcome: PU. Reason: r.",
    }


@pytest.mark.parametrize(
    ("outcome", "priority", "queue", "status"),
    [
        ("CP", REGULAR, INTAKE, MANUAL_PASS),
        ("IN", REGULAR, INTAKE, ADJUDICATE),
        ("PR", REGULAR, ADJUDICATORS, ADJUDICATE),
        ("PU", URGENT, ADJUDICATORS, ADJUDICATE),
        ("TCM", REGULAR, CASE_MANAGERS, ADJUDICATE),
        ("CR", CRITICAL, ADJUDICATORS, ADJUDICATE),
    ],
)
def test_each_outcome_sets_its_priority_queue_and_status(
    outcome, priority, queue, status
):
    body = _sent_body("UPDATE_OUTCOME", {**PAYLOAD, "outcome_code": outcome})
    assert (body["aiProcessedStatus"], body["aiDpsPriority"]) == (PROCESSED, priority)
    assert (body["aiQueue"], body["aiDmerStatus"]) == (queue, status)


def test_a_fallback_is_a_processing_error_sent_to_intake_as_in():
    body = _sent_body(
        "UPDATE_OUTCOME",
        {
            "document_guid": GUID,
            "outcome_code": "IN",
            "decided_by": "FALLBACK",
            "comment": "Unable to AI process due to error. The PDF could not be read.",
        },
    )
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": ERROR,
        "aiDpsPriority": REGULAR,
        "aiQueue": INTAKE,
        "aiDmerStatus": ADJUDICATE,
        "notes": "Unable to AI process due to error. The PDF could not be read.",
    }


def test_a_duplicate_sends_only_the_duplicate_flag_and_note():
    body = _sent_body(
        "UPDATE_OUTCOME",
        {
            **PAYLOAD,
            "outcome_code": "CP",
            "is_duplicate": True,
            "duplicate_of_document_guid": "other-guid",
        },
    )
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": PROCESSED,
        "aiIsDuplicate": True,
        "notes": "Duplicate of DMER other-guid.",
    }


@pytest.mark.parametrize("duplicate", [False, True])
def test_an_outcome_whose_driver_link_failed_goes_to_intake_as_an_error(duplicate):
    body = _sent_body(
        "UPDATE_OUTCOME",
        {
            **PAYLOAD,
            "outcome_code": "PU",
            "comment": "AI outcome: PU. Reason: r.",
            "is_duplicate": duplicate,
            "driver_link_error": "Mercury write POST rejected: Driver not found.",
        },
    )
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": ERROR,
        "aiDpsPriority": REGULAR,
        "aiQueue": INTAKE,
        "aiDmerStatus": ADJUDICATE,
        "notes": (
            "Unable to AI process due to error: the driver could not be linked "
            "to the DMER (Mercury write POST rejected: Driver not found). "
            "AI outcome: PU. Reason: r."
        ),
    }


def test_a_mark_duplicate_row_queued_before_is_sent_as_a_duplicate():
    body = _sent_body(
        "MARK_DUPLICATE",
        {"document_guid": GUID, "status": "Rejected", "comment": "c"},
    )
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": PROCESSED,
        "aiIsDuplicate": True,
        "notes": "Duplicate",
    }


def test_an_outcome_without_routing_is_a_permanent_failure_and_calls_nothing():
    http = FakeHttp()
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox(
            "UPDATE_OUTCOME", {**PAYLOAD, "outcome_code": "XX"}, "k"
        )
    assert err.value.retryable is False
    assert http.calls == []


MAP_DRIVER_PAYLOAD = {
    "document_guid": GUID,
    "comment": "AI outcome: CP.",
    "mercury_driver_id": "D7",
    "licence_number": "01234567",
    "first_name": "FAKE",
    "last_name": "DRIVER",
}
OPEN_CASE = {
    "resultMessage": "Driver successfully linked to document",
    "driverOpenCase": {
        "caseId": "C1",
        "caseTitle": "T",
        "caseType": "Suspected Medical Condition",
        "dmerStatus": "Active",
    },
}


def test_map_driver_posts_add_driver_and_returns_the_open_case():
    http = FakeHttp((200, OPEN_CASE))
    response = _client(http).send_outbox(
        "MAP_DRIVER", MAP_DRIVER_PAYLOAD, "dec-1:MAP_DRIVER"
    )
    assert response == OPEN_CASE  # kept as the row's mercury_response
    ((method, url, headers, body),) = http.calls
    assert (method, url) == (
        "POST",
        f"https://mercury.test/api/dmers/{GUID}/add_driver",
    )
    assert headers["Idempotency-Key"] == "dec-1:MAP_DRIVER"
    assert body == {
        "driverLicenceNumber": "01234567",
        "firstName": "FAKE",
        "surname": "DRIVER",
        "comments": "Driver linked by the AI from the licence number read on the DMER.",
        "dmerId": GUID,
    }


@pytest.mark.parametrize("missing", ["licence_number", "first_name", "last_name"])
def test_map_driver_without_the_drivers_details_is_a_permanent_failure(missing):
    http = FakeHttp()
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox(
            "MAP_DRIVER", {**MAP_DRIVER_PAYLOAD, missing: None}, "k"
        )
    assert err.value.retryable is False
    assert http.calls == []


@pytest.mark.parametrize("operation", ["MAP_DRIVER", "UPDATE_OUTCOME"])
def test_mercurys_error_shape_is_a_rejected_write_even_with_a_2xx(operation):
    payload = MAP_DRIVER_PAYLOAD if operation == "MAP_DRIVER" else PAYLOAD
    http = FakeHttp(
        (200, {"errorStatus": 1, "replyMessage": "Driver not found", "caseId": ""})
    )
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox(operation, payload, "k")
    assert err.value.retryable is False
    assert "Driver not found" in str(err.value)  # kept on the row's last_error


def test_error_status_zero_is_a_success():
    http = FakeHttp((200, {"errorStatus": 0, "replyMessage": "ok"}))
    assert _client(http).send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")["errorStatus"] == 0


CASE_PAYLOAD = {
    "document_guid": GUID,
    "comment": "AI outcome: CP.",
    "mercury_driver_id": "D7",
    "licence_number": "01234567",
    "mercury_case_id": None,
}
LOOKUP_URL = "https://mercury.test/api/drivers/lookup"
ADD_CASE_URL = f"https://mercury.test/api/dmers/{GUID}/add_case"
CREATED = {"caseId": "C9", "documentRecordId": GUID, "status": "Created"}


def test_create_case_is_skipped_when_the_dmer_already_has_a_case():
    http = FakeHttp()
    result = _client(http).send_outbox(
        "CREATE_CASE", {**CASE_PAYLOAD, "mercury_case_id": "C1"}, "k"
    )
    assert result == {"action": "skipped", "reason": "dmer_has_case", "case_id": "C1"}
    assert http.calls == []


@pytest.mark.parametrize(
    "lookup",
    [
        {"driver_id": "D7", "case": {"case_id": "C5", "case_status": "Active"}},
        {"driver_id": "D7", "case": {"case_id": "C5"}},  # no status: open
        [{"driver_id": "D7", "case": {"case_id": "C5"}}],
    ],
)
def test_create_case_is_skipped_when_the_driver_has_an_open_case(lookup):
    http = FakeHttp((200, lookup))
    result = _client(http).send_outbox("CREATE_CASE", CASE_PAYLOAD, "k")
    assert result == {
        "action": "skipped",
        "reason": "driver_has_open_case",
        "case_id": "C5",
    }
    ((method, url, headers, body),) = http.calls
    assert (method, url) == ("POST", LOOKUP_URL)
    assert body == {"driverLicenceNumber": "01234567"}
    assert "Idempotency-Key" not in headers  # a read


@pytest.mark.parametrize(
    "case",
    [None, {"case_id": "C5", "case_status": "Closed"}],
)
def test_create_case_adds_a_case_when_the_driver_has_no_open_one(case):
    http = FakeHttp((200, {"driver_id": "D7", "case": case}), (200, CREATED))
    result = _client(http).send_outbox("CREATE_CASE", CASE_PAYLOAD, "dec-1:CREATE_CASE")
    assert result == {"action": "created", "response": CREATED}
    _, (method, url, headers, body) = http.calls
    assert (method, url) == ("POST", ADD_CASE_URL)
    assert headers["Idempotency-Key"] == "dec-1:CREATE_CASE"
    assert body == {
        "case_type": "DMER",
        "owner": "TBD",
        "driver": {"driver_id": "01234567"},
        "document_type": "DMER",
        "comments": (
            "Case created by the AI: the DMER needed a case and the driver had "
            "no active one."
        ),
    }


REAL_CASE = {
    "caseId": "C5",
    "caseTitle": "01234567- DRIVER- DMER- 1",
    "caseType": "Suspected Medical Condition",
    "dmerStatus": "Active",
}
PLACEHOLDER_CASE = {**REAL_CASE, "caseId": "C0", "caseTitle": "00000000- DFC- DMER- 3"}


def test_after_add_driver_a_real_active_case_means_no_new_case():
    http = FakeHttp()
    result = _client(http).send_outbox(
        "CREATE_CASE",
        {**CASE_PAYLOAD, "driver_linked": True, "driver_open_case": REAL_CASE},
        "k",
    )
    assert result == {
        "action": "skipped",
        "reason": "driver_has_open_case",
        "case_id": "C5",
    }
    assert http.calls == []  # add_driver already answered: no lookup


@pytest.mark.parametrize(
    "open_case",
    [None, {}, PLACEHOLDER_CASE],
    ids=["no-active-case", "empty", "placeholder-case"],
)
def test_after_add_driver_no_case_or_a_placeholder_case_means_a_new_case(open_case):
    # add_driver for a real licence isn't expected to return the placeholder's
    # case (to confirm with Mercury); if it does, the DMER still needs a case.
    http = FakeHttp((200, CREATED))
    result = _client(http).send_outbox(
        "CREATE_CASE",
        {
            **CASE_PAYLOAD,
            # a placeholder DMER arrives on the placeholder driver's case
            "mercury_case_id": "C0",
            "driver_linked": True,
            "driver_open_case": open_case,
        },
        "k",
    )
    assert result == {"action": "created", "response": CREATED}
    ((method, url, _, body),) = http.calls  # straight to add_case
    assert (method, url) == ("POST", ADD_CASE_URL)
    assert body["driver"] == {"driver_id": "01234567"}


def test_a_placeholder_case_in_the_lookup_is_not_an_open_case():
    lookup = {
        "driver_id": "D7",
        "case": {"case_id": "C0", "case_title": "00000000- DFC- DMER- 1"},
    }
    http = FakeHttp((200, lookup), (200, CREATED))
    result = _client(http).send_outbox("CREATE_CASE", CASE_PAYLOAD, "k")
    assert result["action"] == "created"


@pytest.mark.parametrize("status", [404, 409])
def test_add_case_rejected_is_a_permanent_failure(status):
    http = FakeHttp((200, {"driver_id": "D7"}), (status, {"error": "x"}))
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox("CREATE_CASE", CASE_PAYLOAD, "k")
    assert (err.value.status, err.value.retryable) == (status, False)


def test_create_case_without_a_licence_is_a_permanent_failure():
    with pytest.raises(MercuryWriteError) as err:
        _client(FakeHttp()).send_outbox(
            "CREATE_CASE", {**CASE_PAYLOAD, "licence_number": None}, "k"
        )
    assert err.value.retryable is False


def test_an_outcome_whose_case_failed_goes_as_usual_with_the_error_noted():
    body = _sent_body(
        "UPDATE_OUTCOME",
        {
            **PAYLOAD,
            "outcome_code": "TCM",
            "comment": "AI outcome: TCM. Reason: r.",
            "case_error": "Mercury write POST returned HTTP 409",
        },
    )
    assert body == {
        "dmerId": GUID,
        "aiProcessedStatus": PROCESSED,
        "aiDpsPriority": REGULAR,
        "aiQueue": CASE_MANAGERS,
        "aiDmerStatus": ADJUDICATE,
        "notes": (
            "AI outcome: TCM. Reason: r. The case could not be created for this "
            "DMER (Mercury write POST returned HTTP 409)."
        ),
    }


@pytest.mark.parametrize(
    "status, retryable",
    [(500, True), (503, True), (429, True), (408, True), (400, False), (409, False)],
)
def test_failures_say_whether_a_retry_can_help(status, retryable):
    http = FakeHttp((status, {"error": "x"}))
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
    assert (err.value.status, err.value.retryable) == (status, retryable)
    assert len(http.calls) == 1  # no in-call retry: the publisher backs off


def test_a_network_error_is_retryable():
    http = FakeHttp(OSError("connection reset"))
    with pytest.raises(MercuryWriteError) as err:
        _client(http).send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
    assert err.value.retryable is True and err.value.status is None


def test_an_open_breaker_is_retryable_and_calls_nothing():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=3600)
    http = FakeHttp((503, None))
    client = _client(http, breaker)
    with pytest.raises(MercuryWriteError):
        client.send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
    with pytest.raises(MercuryWriteError) as err:
        client.send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
    assert err.value.retryable is True
    assert len(http.calls) == 1


def test_a_rejected_write_does_not_trip_the_breaker():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout=3600)
    http = FakeHttp((400, None), (200, {}))
    client = _client(http, breaker)
    with pytest.raises(MercuryWriteError):
        client.send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
    client.send_outbox("UPDATE_OUTCOME", PAYLOAD, "k")
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
            {**PAYLOAD, "comment": "CLINICAL DETAIL"},
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
