"""Rule Engine activity with DECISION_ENGINE=triage_llm (off by default; the
rules engine is the default): the triage LLM evaluates the normalized DMER, and
the result is recorded exactly as the rules engine's is. The policy loader, the model call and the database
are stubbed; the outcome selection is the real select_outcome."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import workflow_orchestrator.activities.rule_engine as activity
from dmer_common.db.rule_engine import DriverCount, RuleEngineRun
from dmer_common.normalization import NormalizationValidationError
from dmer_common.rules import Outcome, select_outcome
from dmer_common.triage import PolicyText, PolicyUnavailableError, TriageResult

DOC_ID = "123e4567-e89b-12d3-a456-426655440000"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
NORMALIZED_URL = f"https://blob/normalized-dmer/{DOC_ID}/runs/3/normalized.json"
INPUT = {"document_id": DOC_ID, "normalized_blob_url": NORMALIZED_URL}
RUN_ID = 11
NORMALIZED = {"first_name": "Mary", "cardiovascular.atrial_fibrillation": True}
POLICY = PolicyText(
    text="PROCEDURES", pdf_sha256="ab" * 32, pdf_etag='"0x1"', updated=date(2026, 6, 12)
)
ANSWER = {
    "evaluation_status": "RULE_MATCHED",
    "conditions_evaluated": [
        {
            "condition": "Atrial fibrillation",
            "rule_status": "APPLIED",
            "recommended_action": "PR",
            "evidence": ['details_of_condition: "AF, dizzy spells"'],
            "reasoning": "AF with symptoms",
        }
    ],
}


def _result(action: str = "PR") -> TriageResult:
    evaluation = select_outcome(
        (Outcome("Cardiovascular", action, False, "Arrhythmia"),)
    )
    return TriageResult(
        evaluation=evaluation,
        answer=ANSWER,
        checks=[],
        policy_sha256=POLICY.pdf_sha256,
        policy_updated=POLICY.updated,
    )


@pytest.fixture
def d(monkeypatch):
    repository = SimpleNamespace(
        start_or_resume=AsyncMock(
            return_value=RuleEngineRun(
                RUN_ID, 1, datetime(2026, 9, 1, tzinfo=UTC), DRIVER_KEY
            )
        ),
        find_rules_version=AsyncMock(),
        rules_version_registered=AsyncMock(return_value=True),
        succeed=AsyncMock(return_value=(501, DriverCount(True, "eval-1"))),
        fail=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    blob = Mock()
    blob.blob_url.side_effect = lambda container, path: (
        f"https://blob/{container}/{path}"
    )
    blob.download.side_effect = lambda url: {
        NORMALIZED_URL: json.dumps(NORMALIZED).encode()
    }[url]
    blob.upload_json.side_effect = lambda container, path, obj: (
        f"https://blob/{container}/{path}"
    )
    triage = Mock(return_value=_result())

    @asynccontextmanager
    async def session(_engine, _document_id):
        yield repository

    monkeypatch.setattr(activity, "rule_engine_session", session)
    monkeypatch.setattr(activity, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(activity, "BlobClient", lambda _: blob)
    monkeypatch.setattr(activity, "load_policy", Mock(return_value=POLICY))
    monkeypatch.setattr(activity, "triage_document", triage)
    monkeypatch.setattr(activity, "openai_settings", Mock())
    monkeypatch.setattr(activity, "OpenAIClient", Mock())
    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://blob")
    monkeypatch.setenv("DECISION_ENGINE", "triage_llm")  # chosen explicitly
    monkeypatch.delenv("NORMALIZED_DMER_CONTAINER", raising=False)
    return SimpleNamespace(repo=repository, blob=blob, triage=triage, engine=engine)


def _run(payload=INPUT):
    return asyncio.run(activity.run_rule_engine_activity(payload))


def test_unset_decision_engine_means_the_rules_engine(d, monkeypatch):
    # The triage LLM must never run unless DECISION_ENGINE says so.
    monkeypatch.delenv("DECISION_ENGINE", raising=False)
    d.blob.download.side_effect = Exception("no rules.json here")
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.triage.assert_not_called()
    assert d.blob.blob_url.call_args.args == ("rules", "active/rules.json")


def test_the_triage_llm_when_chosen_is_recorded_like_the_rules(d):
    result = _run()

    assert result == {
        "rule_evaluation_id": 501,
        "rules_version": "2026-06-12",
        "selected_outcome_code": "PR",
    }
    d.repo.find_rules_version.assert_not_awaited()  # rules.json is not read
    d.repo.rules_version_registered.assert_awaited_once_with("2026-06-12")
    kwargs = d.repo.succeed.call_args.kwargs
    assert d.repo.succeed.call_args.args == (RUN_ID,)
    assert kwargs["rules_version_name"] == "2026-06-12"
    assert kwargs["driver_key"] == DRIVER_KEY
    assert kwargs["evaluation"].selected_outcome_code == "PR"
    d.repo.fail.assert_not_awaited()
    d.engine.dispose.assert_awaited_once()


def test_the_triage_llm_gets_the_normalized_dmer_and_the_active_policy(d):
    _run()

    args, kwargs = d.triage.call_args
    assert args[1] == NORMALIZED and args[2] is POLICY
    assert isinstance(kwargs["evaluation_date"], date)


def test_the_full_answer_goes_to_the_blob_and_only_the_record_to_the_database(d):
    _run()

    container, path, stored = d.blob.upload_json.call_args.args
    assert (container, path) == (
        "normalized-dmer",
        f"{DOC_ID}/runs/{RUN_ID}/triage.json",
    )
    assert stored["answer"] == ANSWER
    record = d.repo.succeed.call_args.kwargs["all_outcomes"]
    assert record["engine"] == "triage_llm"
    assert record["answer_blob_url"] == f"https://blob/normalized-dmer/{path}"
    assert record["policy_sha256"] == POLICY.pdf_sha256
    assert "dizzy" not in json.dumps(record) and "answer" not in record


@pytest.mark.parametrize("engine", ["rules", "RULES"])
def test_rules_switches_back_to_rules_json(d, engine, monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE", engine)
    d.blob.download.side_effect = Exception("no rules.json here")
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.triage.assert_not_called()


def test_an_unknown_engine_fails_every_document_loudly(d, monkeypatch):
    monkeypatch.setenv("DECISION_ENGINE", "gpt")
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.triage.assert_not_called()
    assert d.repo.fail.call_args.kwargs["error_code"] == "RulesetUnavailableError"


def test_an_unreadable_procedures_pdf_is_unavailable(d, monkeypatch):
    monkeypatch.setattr(
        activity, "load_policy", Mock(side_effect=PolicyUnavailableError("no PDF"))
    )
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.triage.assert_not_called()
    d.repo.succeed.assert_not_awaited()


def test_an_unregistered_procedures_version_is_unavailable(d):
    # rules_version is written by maintenance only, as for rules.json.
    d.repo.rules_version_registered.return_value = False
    with pytest.raises(activity.RulesetUnavailableError):
        _run()
    d.triage.assert_not_called()


def test_an_answer_still_invalid_after_retries_is_poison(d):
    d.triage.side_effect = NormalizationValidationError("still invalid")
    with pytest.raises(activity.RuleEngineValidationError):
        _run()
    assert d.repo.fail.call_args.kwargs["error_code"] == "RuleEngineValidationError"
    assert activity.RuleEngineValidationError in activity.POISON
