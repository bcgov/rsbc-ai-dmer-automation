"""Signal Driver and Route To Manual Review activities, and poison results.

Database and Service Bus I/O are mocked; the status writes themselves are
tested against PostgreSQL in libs/dmer_common/tests/integration.
"""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dmer_common.db.document_routing import DocumentSignalState
from dmer_common.dto import DRIVER_DECISION_EVENT, event_message_id

import workflow_orchestrator.activities.manual_review as review
import workflow_orchestrator.activities.signal_driver as signal
from workflow_orchestrator.activities._runtime import poison_as_result

DOC_ID = "123e4567-e89b-12d3-a456-426655440000"
GUID = "9d2c3b1a-0000-4000-8000-000000000001"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
NORMALIZED_URL = f"https://blob/normalized-dmer/{DOC_ID}/runs/3/normalized.json"
INPUT = {"document_id": DOC_ID, "normalized_blob_url": NORMALIZED_URL}


@pytest.fixture
def signal_deps(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    state = AsyncMock(
        return_value=DocumentSignalState(GUID, DRIVER_KEY, "RULES_APPLIED")
    )
    awaiting = AsyncMock(return_value=True)
    publish = Mock()
    monkeypatch.setattr(signal, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(signal, "load_signal_state", state)
    monkeypatch.setattr(signal, "mark_awaiting_driver_completion", awaiting)
    monkeypatch.setattr(signal, "publish_driver_decision", publish)
    return SimpleNamespace(
        engine=engine, state=state, awaiting=awaiting, publish=publish
    )


def _signal(payload=INPUT):
    return asyncio.run(signal.signal_driver_activity(payload))


def test_publishes_a_deterministic_driver_decision_then_marks_awaiting(signal_deps):
    d = signal_deps
    message_id = event_message_id(DRIVER_DECISION_EVENT, DOC_ID)
    assert _signal() == {"published": True, "message_id": message_id}
    message = d.publish.call_args.args[0]
    assert message.message_id == message_id
    assert message.driver_key == DRIVER_KEY
    assert message.document_guid == GUID
    assert message.blob_url == NORMALIZED_URL
    body = json.loads(message.model_dump_json(by_alias=True))
    assert set(body) == {
        "messageId",
        "documentId",
        "schemaVersion",
        "documentGuid",
        "driverKey",
        "blobUrl",
        "attempt",
        "enqueuedAt",
    }  # pointers only
    d.awaiting.assert_awaited_once()
    d.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize(
    "status", ["AWAITING_DRIVER_COMPLETION", "DECIDED", "COMPLETED"]
)
def test_an_already_signalled_document_is_not_published_again(signal_deps, status):
    d = signal_deps
    d.state.return_value = DocumentSignalState(GUID, DRIVER_KEY, status)
    assert _signal() == {"published": False}
    d.publish.assert_not_called()


@pytest.mark.parametrize(
    "state",
    [
        None,
        DocumentSignalState(GUID, None, "RULES_APPLIED"),
        DocumentSignalState(GUID, DRIVER_KEY, "NORMALIZED"),
    ],
)
def test_a_document_that_cant_be_signalled_is_poison(signal_deps, state):
    d = signal_deps
    d.state.return_value = state
    with pytest.raises(signal.SignalDriverValidationError):
        _signal()
    d.publish.assert_not_called()
    assert asyncio.run(
        poison_as_result(signal.signal_driver_activity(INPUT), signal.POISON)
    ) == {"poison": True, "error_code": "SignalDriverValidationError"}


def test_a_publish_failure_is_retried_not_poison(signal_deps):
    d = signal_deps
    d.publish.side_effect = ConnectionError("private broker detail")
    with pytest.raises(signal.SignalDriverActivityError) as error:
        asyncio.run(
            poison_as_result(signal.signal_driver_activity(INPUT), signal.POISON)
        )
    assert "private broker detail" not in str(error.value)
    d.awaiting.assert_not_awaited()


def test_publish_uses_the_driver_key_as_session_and_env_for_the_queue(monkeypatch):
    import sys

    sent, clients = [], []

    class Sender:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send_messages(self, message):
            sent.append(message)

    class Client:
        def __init__(self, namespace, credential):
            clients.append(namespace)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get_queue_sender(self, queue):
            clients.append(queue)
            return Sender()

    fake_sb = SimpleNamespace(
        ServiceBusClient=Client, ServiceBusMessage=lambda body, **kw: kw
    )
    fake_identity = SimpleNamespace(
        DefaultAzureCredential=lambda: SimpleNamespace(close=lambda: None)
    )
    monkeypatch.setitem(sys.modules, "azure.servicebus", fake_sb)
    monkeypatch.setitem(sys.modules, "azure.identity", fake_identity)
    monkeypatch.setenv(
        "SERVICE_BUS_NAMESPACE_FQDN", "sb.example.servicebus.windows.net"
    )
    monkeypatch.setenv("DRIVER_DECISION_QUEUE", "driver-decision-test")
    from datetime import UTC, datetime

    from dmer_common.dto import DriverDecisionMessage

    message = DriverDecisionMessage(
        message_id="m-1",
        document_id=DOC_ID,
        document_guid=GUID,
        driver_key=DRIVER_KEY,
        blob_url=NORMALIZED_URL,
        enqueued_at=datetime(2026, 10, 2, tzinfo=UTC),
    )
    signal.publish_driver_decision(message)
    assert clients == ["sb.example.servicebus.windows.net", "driver-decision-test"]
    assert sent[0]["session_id"] == DRIVER_KEY
    assert sent[0]["message_id"] == "m-1"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_id": DOC_ID},
        {"document_id": "x", "normalized_blob_url": NORMALIZED_URL},
    ],
)
def test_bad_signal_input_is_poison(signal_deps, payload):
    with pytest.raises(signal.SignalDriverValidationError):
        _signal(payload)
    signal_deps.state.assert_not_awaited()


# ---------------------------------------------------------------------------
# Route To Manual Review
# ---------------------------------------------------------------------------


@pytest.fixture
def review_deps(monkeypatch):
    engine = SimpleNamespace(dispose=AsyncMock())
    route = AsyncMock(return_value=True)
    monkeypatch.setattr(review, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(review, "route_to_manual_review", route)
    return SimpleNamespace(engine=engine, route=route)


def test_routes_with_the_reason_code(review_deps):
    payload = {
        "document_id": DOC_ID,
        "stage": "NORMALIZE",
        "category": "UNKNOWN",
        "reason": "NormalizeDmer:RETRIES_EXHAUSTED",
    }
    assert asyncio.run(review.route_to_manual_review_activity(payload)) == {
        "routed": True
    }
    kwargs = review_deps.route.call_args.kwargs
    assert kwargs["reason_code"] == "NormalizeDmer:RETRIES_EXHAUSTED"
    assert kwargs["stage"] == "NORMALIZE"
    assert kwargs["category"] == "UNKNOWN"
    assert "NormalizeDmer" not in kwargs["message"]  # a fixed description
    review_deps.engine.dispose.assert_awaited_once()


def test_an_already_terminal_document_is_reported_not_rerouted(review_deps):
    review_deps.route.return_value = False
    payload = {
        "document_id": DOC_ID,
        "stage": "RULES",
        "category": "UNKNOWN",
        "reason": "RunRuleEngine:RETRIES_EXHAUSTED",
    }
    assert asyncio.run(review.route_to_manual_review_activity(payload)) == {
        "routed": False
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"document_id": DOC_ID, "stage": "RULES", "category": "UNKNOWN"},
        {
            "document_id": DOC_ID,
            "stage": "RULES",
            "category": "UNKNOWN",
            "reason": "free text",
        },
        {"document_id": "nope", "stage": "RULES", "category": "UNKNOWN", "reason": "X"},
        {
            "document_id": DOC_ID,
            "stage": "MADE_UP",
            "category": "UNKNOWN",
            "reason": "X",
        },
        {"document_id": DOC_ID, "stage": "RULES", "category": "MADE_UP", "reason": "X"},
    ],
)
def test_a_reason_must_be_a_code(review_deps, payload):
    with pytest.raises(review.ManualReviewValidationError):
        asyncio.run(review.route_to_manual_review_activity(payload))
    review_deps.route.assert_not_awaited()


# ---------------------------------------------------------------------------
# Poison results
# ---------------------------------------------------------------------------


def test_poison_errors_become_a_result_with_the_type_only():
    async def boom():
        raise ValueError("private document content")

    assert asyncio.run(poison_as_result(boom(), (ValueError,))) == {
        "poison": True,
        "error_code": "ValueError",
    }


def test_other_errors_still_raise_for_the_retry_policy():
    async def boom():
        raise ConnectionError("transient")

    with pytest.raises(ConnectionError):
        asyncio.run(poison_as_result(boom(), (ValueError,)))


@pytest.mark.parametrize(
    "module,name",
    [
        ("driver_lookup", "DriverLookup"),
        ("normalize", "NormalizeDmer"),
        ("rule_engine", "RunRuleEngine"),
        ("signal_driver", "SignalDriver"),
    ],
)
def test_registered_activities_return_poison_results(monkeypatch, module, name):
    import importlib

    import azure.durable_functions as df

    mod = importlib.import_module(f"workflow_orchestrator.activities.{module}")
    poison = mod.POISON[0]

    async def fails(payload):
        raise poison.__new__(poison)

    activity_fn = next(
        n for n in dir(mod) if n.endswith("_activity") and callable(getattr(mod, n))
    )
    monkeypatch.setattr(mod, activity_fn, fails)
    app = df.DFApp()
    mod.register(app)
    (function,) = app.get_functions()
    assert function.get_function_name() == name
    result = asyncio.run(function.get_user_function()({"document_id": DOC_ID}))
    assert result == {"poison": True, "error_code": poison.__name__}
