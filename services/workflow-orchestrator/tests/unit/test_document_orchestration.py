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
    """Records each call_activity invocation; yields a sentinel Task marker
    (never the resolved value) -- matching what a real DurableOrchestrationContext
    hands back to the generator before the harness sends the resolved value in."""

    def __init__(self, trigger_input: dict) -> None:
        self._input = trigger_input
        self.calls: list[tuple[str, dict]] = []

    def get_input(self) -> dict:
        return self._input

    def call_activity(self, name: str, input_: dict | None = None):
        self.calls.append((name, input_))
        return ("TASK", name, input_)


def test_calls_normalize_with_only_document_id_and_extracted_blob_url():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)

    task = next(gen)

    assert task == (
        "TASK",
        "NormalizeDmer",
        {
            "document_id": INPUT["document_id"],
            "extracted_blob_url": INPUT["extracted_blob_url"],
        },
    )
    assert context.calls == [task[1:]]


def test_runs_the_rule_engine_on_the_normalized_blob_and_returns_its_result():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)

    task = gen.send({"normalized_blob_url": "https://blob/normalized-dmer/doc.json"})
    assert task == (
        "TASK",
        "RunRuleEngine",
        {
            "document_id": INPUT["document_id"],
            "normalized_blob_url": "https://blob/normalized-dmer/doc.json",
        },
    )

    result = {
        "rule_evaluation_id": 7,
        "rules_version": "v1",
        "selected_outcome_code": "CP",
    }
    with pytest.raises(StopIteration) as exc:
        gen.send(result)
    assert exc.value.value == result
    assert [name for name, _ in context.calls] == ["NormalizeDmer", "RunRuleEngine"]


def test_activities_get_ids_and_blob_urls_only():
    context = _FakeContext(INPUT)
    gen = orch.document_orchestration(context)
    next(gen)
    gen.send({"normalized_blob_url": "https://blob/normalized-dmer/doc.json"})

    for _, activity_input in context.calls:
        assert "driver_key" not in activity_input
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
