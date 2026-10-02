"""Document Orchestration's own logic: what it calls, with what input, and
what it returns (external I/O -- Durable's activity dispatch -- mocked by
driving the generator directly, same technique the Durable Functions Python
worker itself uses to run an orchestrator)."""

import json

import pytest

import workflow_orchestrator.orchestrators.document_orchestration as orch

INPUT = {
    "document_id": "123e4567-e89b-12d3-a456-426655440000",
    "driver_key": "74f5a6ce-575a-4689-a553-99f50c24ca15",
    "extracted_blob_url": "https://blob/extracted-dmer/doc.json",
}


class _FakeContext:
    """Records each activity call; yields a sentinel Task marker (never the
    resolved value) -- matching what a real DurableOrchestrationContext hands
    back to the generator before the harness sends the resolved value in."""

    def __init__(self, trigger_input: dict) -> None:
        self._input = trigger_input
        self.calls: list[tuple[str, dict]] = []
        self.retry_options = []

    def get_input(self) -> dict:
        return self._input

    def call_activity_with_retry(
        self, name: str, retry_options, input_: dict | None = None
    ):
        self.calls.append((name, input_))
        self.retry_options.append(retry_options)
        return ("TASK", name, input_)


DOC = INPUT["document_id"]
DRIVER = {"driver_key": INPUT["driver_key"]}
NORMALIZED = {"normalized_blob_url": "https://blob/normalized-dmer/doc.json"}
RULES = {"rule_evaluation_id": 7, "rules_version": "v1", "selected_outcome_code": "CP"}
SIGNALLED = {"published": True, "message_id": "m-1"}


def _names(context):
    return [name for name, _ in context.calls]


def _finish(gen, value):
    with pytest.raises(StopIteration) as exc:
        gen.send(value)
    return exc.value.value


def test_full_run_resolves_normalizes_evaluates_then_signals_the_driver():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)

    assert next(gen) == ("TASK", "DriverLookup", {"document_id": DOC})
    assert gen.send(DRIVER) == (
        "TASK",
        "NormalizeDmer",
        {"document_id": DOC, "extracted_blob_url": INPUT["extracted_blob_url"]},
    )
    assert gen.send(NORMALIZED) == (
        "TASK",
        "RunRuleEngine",
        {"document_id": DOC, "normalized_blob_url": NORMALIZED["normalized_blob_url"]},
    )
    assert gen.send(RULES) == (
        "TASK",
        "SignalDriver",
        {"document_id": DOC, "normalized_blob_url": NORMALIZED["normalized_blob_url"]},
    )
    assert _finish(gen, SIGNALLED) == {
        **RULES,
        "driver_key": INPUT["driver_key"],
        "driver_signalled": True,
    }


def test_every_activity_uses_the_retry_settings_from_the_input():
    context = _FakeContext(
        {**INPUT, "retry": {"first_retry_interval_ms": 1500, "max_attempts": 4}}
    )
    gen = orch.document_orchestration(context)
    next(gen)
    gen.send(DRIVER)
    options = context.retry_options
    assert all(o is options[0] for o in options)
    assert options[0].first_retry_interval_in_milliseconds == 1500
    assert options[0].max_number_of_attempts == 4


def test_default_retry_settings_when_the_input_has_none():
    context = _FakeContext(INPUT)
    next(orch.document_orchestration(context))
    assert (
        context.retry_options[0].max_number_of_attempts
        == orch.DEFAULT_RETRY["max_attempts"]
    )


def test_a_document_driver_lookup_routed_to_manual_review_ends_there():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)
    manual = {"manual_review": True, "reason": "DRIVER_NOT_FOUND"}
    assert _finish(gen, manual) == manual
    assert _names(context) == ["DriverLookup"]


@pytest.mark.parametrize(
    "steps,failing",
    [
        ([], "DriverLookup"),
        ([DRIVER], "NormalizeDmer"),
        ([DRIVER, NORMALIZED], "RunRuleEngine"),
        ([DRIVER, NORMALIZED, RULES], "SignalDriver"),
    ],
)
def test_a_poison_result_routes_to_manual_review_and_stops(steps, failing):
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)
    for value in steps:
        gen.send(value)
    task = gen.send({"poison": True, "error_code": "SomeValidationError"})
    reason = f"{failing}:SomeValidationError"
    assert task == (
        "TASK",
        "RouteToManualReview",
        {
            "document_id": DOC,
            "stage": orch.ACTIVITY_STAGES[failing],
            "category": "PERMANENT_BUSINESS",
            "reason": reason,
        },
    )
    assert _finish(gen, {"routed": True}) == {"manual_review": True, "reason": reason}
    assert _names(context)[-2:] == [failing, "RouteToManualReview"]


@pytest.mark.parametrize(
    "steps,failing", [([], "DriverLookup"), ([DRIVER, NORMALIZED], "RunRuleEngine")]
)
def test_retries_exhausted_routes_to_manual_review(steps, failing):
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)
    for value in steps:
        gen.send(value)
    task = gen.throw(Exception("Activity function failed: private detail"))
    reason = f"{failing}:RETRIES_EXHAUSTED"
    assert task == (
        "TASK",
        "RouteToManualReview",
        {
            "document_id": DOC,
            "stage": orch.ACTIVITY_STAGES[failing],
            "category": "UNKNOWN",
            "reason": reason,
        },
    )
    assert "private detail" not in repr(context.calls)
    assert _finish(gen, {"routed": True}) == {"manual_review": True, "reason": reason}


def test_activities_get_ids_blob_urls_and_reason_codes_only():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)
    gen.send(DRIVER)
    gen.send(NORMALIZED)
    gen.send(RULES)
    for _, activity_input in context.calls:
        assert set(activity_input) <= {
            "document_id",
            "extracted_blob_url",
            "normalized_blob_url",
        }


def test_functions_host_registers_orchestrator_by_its_name():
    import azure.durable_functions as df

    app = df.DFApp()
    orch.register(app)
    functions = app.get_functions()
    assert [f.get_function_name() for f in functions] == [orch.ORCHESTRATION_NAME]
    bindings = json.loads(functions[0].get_function_json())["bindings"]
    assert any(
        b["type"] == "orchestrationTrigger" and b["name"] == "context" for b in bindings
    )


def test_every_activity_has_a_stage_for_its_failures():
    assert orch.ACTIVITY_STAGES == {
        "DriverLookup": "DRIVER_LOOKUP",
        "NormalizeDmer": "NORMALIZE",
        "RunRuleEngine": "RULES",
        "SignalDriver": "DECISION",
    }
