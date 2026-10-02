"""Rule Engine activity: ruleset/artifact I/O, persistence and failure contract.

Blob, database and engine I/O are mocked; the evaluation runs the real
``dmer_common.rules`` engine against the repository's real rules.json.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dmer_common.db.rule_engine import (
    CompletedEvaluation,
    DriverCount,
    RuleEngineBusyError,
    RuleEngineRun,
    RuleEngineStateError,
)

import workflow_orchestrator.activities.rule_engine as activity

DOC_ID = "123e4567-e89b-12d3-a456-426655440000"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
NORMALIZED_URL = f"https://blob/normalized-dmer/{DOC_ID}/runs/3/normalized.json"
INPUT = {"document_id": DOC_ID, "normalized_blob_url": NORMALIZED_URL}
RULES_URL = "https://blob/rules/active/rules.json"
RULES_JSON = (
    Path(__file__).resolve().parents[4] / "services/rule-engine/rules/rules.json"
).read_bytes()
RECEIVED = datetime(2026, 9, 1, tzinfo=UTC)


def normalized(**di_fields) -> bytes:
    """A real Normalize output for a DI document with *di_fields* set (the
    model call stubbed out), so the rules see exactly what Normalize writes."""
    from dmer_common.normalization import pipeline

    fields = {
        "details_of_condition": "",
        "current_license_class": "5",
        "medical_examination_date": "2026-08-15",
        "type": "AGE",
        **{k.replace("__", "."): v for k, v in di_fields.items()},
    }
    original = pipeline.analyze_conditions
    pipeline.analyze_conditions = lambda *_: {"dmer": {}}
    try:
        return json.dumps(pipeline.normalize_document(Mock(), fields)).encode()
    finally:
        pipeline.analyze_conditions = original


@pytest.fixture
def dependencies(monkeypatch):
    repository = SimpleNamespace(
        start_or_resume=AsyncMock(
            return_value=RuleEngineRun(11, 1, RECEIVED, DRIVER_KEY)
        ),
        register_rules_version=AsyncMock(return_value="v2026.09"),
        succeed=AsyncMock(return_value=(501, DriverCount(True, "eval-1"))),
        fail=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    blobs = {RULES_URL: RULES_JSON, NORMALIZED_URL: normalized()}
    blob = Mock()
    blob.blob_url.side_effect = (
        lambda container, path: f"https://blob/{container}/{path}"
    )
    blob.download.side_effect = lambda url: blobs[url]

    @asynccontextmanager
    async def session(received_engine, document_id):
        assert received_engine is engine
        assert document_id == DOC_ID
        yield repository

    monkeypatch.setattr(activity, "rule_engine_session", session)
    monkeypatch.setattr(activity, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(activity, "BlobClient", lambda _: blob)
    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://blob")
    monkeypatch.delenv("RULES_CONTAINER", raising=False)
    monkeypatch.delenv("RULES_ACTIVE_PATH", raising=False)
    return SimpleNamespace(repo=repository, engine=engine, blob=blob, blobs=blobs)


def _run(payload=INPUT):
    return asyncio.run(activity.run_rule_engine_activity(payload))


def test_evaluates_the_normalized_document_and_records_every_candidate(dependencies):
    d = dependencies
    d.blobs[NORMALIZED_URL] = normalized(
        cardiovascular__cad="selected", cardiovascular__cad_has_concerns="selected"
    )

    result = _run()

    assert result == {
        "rule_evaluation_id": 501,
        "rules_version": "v2026.09",
        "selected_outcome_code": result["selected_outcome_code"],
    }
    kwargs = d.repo.succeed.call_args.kwargs
    assert d.repo.succeed.call_args.args == (11,)
    assert kwargs["rules_version_name"] == "v2026.09"
    assert kwargs["driver_key"] == DRIVER_KEY
    evaluation = kwargs["evaluation"]
    assert evaluation.selected_outcome_code == result["selected_outcome_code"]
    assert evaluation.outcomes, "a CAD with concerns fires at least one rule"
    d.repo.fail.assert_not_awaited()
    d.engine.dispose.assert_awaited_once()


def test_reads_the_active_ruleset_and_registers_its_checksum(dependencies):
    import hashlib

    d = dependencies
    _run()
    d.blob.blob_url.assert_called_with("rules", "active/rules.json")
    kwargs = d.repo.register_rules_version.call_args.kwargs
    assert kwargs["checksum"] == hashlib.sha256(RULES_JSON).hexdigest()
    assert kwargs["blob_url"] == RULES_URL


def test_blank_document_selects_intake(dependencies):
    # No rule fires -> IN (05-activity-rule-engine.md).
    assert _run()["selected_outcome_code"] == "IN"


def test_committed_evaluation_is_returned_without_reevaluating(dependencies):
    d = dependencies
    d.repo.start_or_resume.return_value = RuleEngineRun(
        11, 1, RECEIVED, DRIVER_KEY, CompletedEvaluation(77, "v1", "CP")
    )
    assert _run() == {
        "rule_evaluation_id": 77,
        "rules_version": "v1",
        "selected_outcome_code": "CP",
    }
    d.blob.download.assert_not_called()
    d.repo.succeed.assert_not_awaited()


def test_invalid_active_ruleset_is_poison_and_recorded(dependencies):
    d = dependencies
    d.blobs[RULES_URL] = b"{not json"
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.repo.fail.assert_awaited_once()
    assert d.repo.fail.call_args.kwargs["error_code"] == "RulesetUnavailableError"
    d.repo.succeed.assert_not_awaited()


def test_missing_active_ruleset_is_poison(dependencies):
    d = dependencies
    del d.blobs[RULES_URL]
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.repo.succeed.assert_not_awaited()


def test_unreadable_normalized_artifact_is_poison(dependencies):
    d = dependencies
    d.blobs[NORMALIZED_URL] = b'["not", "an object"]'
    with pytest.raises(activity.RuleEngineValidationError):
        _run()
    d.repo.fail.assert_awaited_once()
    d.repo.succeed.assert_not_awaited()


def test_document_without_received_date_is_poison(dependencies):
    d = dependencies
    d.repo.start_or_resume.return_value = RuleEngineRun(11, 1, None, DRIVER_KEY)
    with pytest.raises(activity.RuleEngineValidationError):
        _run()
    d.blob.download.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"document_id": "not-a-uuid", "normalized_blob_url": NORMALIZED_URL},
        {"document_id": DOC_ID, "normalized_blob_url": "not a url"},
        "not an object",
    ],
)
def test_bad_input_is_poison_before_any_io(dependencies, payload):
    with pytest.raises(activity.RuleEngineValidationError):
        _run(payload)
    dependencies.repo.start_or_resume.assert_not_awaited()


def test_state_errors_pass_through_unchanged(dependencies):
    d = dependencies
    for error in (RuleEngineBusyError(), RuleEngineStateError()):
        d.repo.start_or_resume.side_effect = error
        with pytest.raises(type(error)):
            _run()


def test_operational_failure_is_sanitized(dependencies):
    d = dependencies
    d.repo.succeed.side_effect = ConnectionError("private SQL parameters")
    with pytest.raises(activity.RuleEngineActivityError) as error:
        _run()
    assert "private SQL parameters" not in str(error.value)
    d.repo.fail.assert_awaited_once()


def test_missing_driver_evaluation_still_completes(dependencies):
    d = dependencies
    d.repo.succeed.return_value = (501, DriverCount(False, None))
    assert _run()["rule_evaluation_id"] == 501


def test_functions_host_registers_activity_by_its_orchestration_name():
    import azure.durable_functions as df

    app = df.DFApp()
    activity.register(app)
    functions = app.get_functions()
    assert [f.get_function_name() for f in functions] == ["RunRuleEngine"]
    bindings = json.loads(functions[0].get_function_json())["bindings"]
    assert any(
        b["type"] == "activityTrigger" and b["name"] == "payload" for b in bindings
    )
