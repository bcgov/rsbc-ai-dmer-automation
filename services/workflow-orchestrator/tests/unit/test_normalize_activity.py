"""Normalize's storage boundary and failure/replay contract (external I/O mocked)."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import workflow_orchestrator.activities.normalize as activity
from dmer_common.db.normalization import (
    NormalizationBusyError,
    NormalizationRun,
    NormalizationStateError,
)
from dmer_common.normalization import (
    NORMALIZATION_SCHEMA_VERSION,
    NormalizationValidationError,
)

DOC_ID = "123e4567-e89b-12d3-a456-426655440000"
INPUT = {"document_id": DOC_ID, "extracted_blob_url": "https://blob/extracted/doc.json"}


@pytest.fixture
def dependencies(monkeypatch):
    repository = SimpleNamespace(
        start_or_resume=AsyncMock(return_value=NormalizationRun(42, 2, None)),
        succeed=AsyncMock(),
        fail=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    blob = Mock()
    blob.download.return_value = json.dumps(
        {"fields": {"vision.cataracts": "true"}}
    ).encode()
    blob.upload_json.side_effect = (
        lambda container, path, obj: f"https://blob/{container}/{path}"
    )
    normalize = Mock(return_value={"vision.cataracts": True})
    client_factory = Mock(return_value=object())

    @asynccontextmanager
    async def session(received_engine, document_id):
        assert received_engine is engine
        assert document_id == DOC_ID
        yield repository

    monkeypatch.setattr(activity, "normalization_session", session)
    monkeypatch.setattr(activity, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(
        activity, "openai_settings", lambda: SimpleNamespace(deployment="test-model")
    )
    monkeypatch.setattr(activity, "BlobClient", lambda _: blob)
    monkeypatch.setattr(activity, "OpenAIClient", client_factory)
    monkeypatch.setattr(activity, "normalize_document", normalize)
    monkeypatch.setenv("BLOB_ACCOUNT_URL", "https://blob")
    monkeypatch.setenv("NORMALIZED_DMER_CONTAINER", "normalized-dmer")
    return SimpleNamespace(
        repo=repository,
        engine=engine,
        blob=blob,
        normalize=normalize,
        client_factory=client_factory,
    )


def test_success_persists_versioned_artifact_before_atomic_completion(dependencies):
    d = dependencies

    async def complete(run_id, **kwargs):
        assert run_id == 42
        assert d.blob.upload_json.call_count == 1
        assert kwargs["output_blob_url"].endswith(f"{DOC_ID}/runs/42/normalized.json")
        assert kwargs["model_version"] == f"test-model@{NORMALIZATION_SCHEMA_VERSION}"

    d.repo.succeed.side_effect = complete
    result = asyncio.run(activity.normalize_dmer_activity(INPUT))
    assert set(result) == {"normalized_blob_url"}
    assert result["normalized_blob_url"].endswith("runs/42/normalized.json")
    d.repo.succeed.assert_awaited_once()
    d.repo.fail.assert_not_awaited()
    d.engine.dispose.assert_awaited_once()


def test_completed_retry_returns_original_url_without_model_or_blob_io(dependencies):
    d = dependencies
    old_url = "https://blob/normalized-dmer/previous-version.json"
    d.repo.start_or_resume.return_value = NormalizationRun(20, 1, old_url)
    assert asyncio.run(activity.normalize_dmer_activity(INPUT)) == {
        "normalized_blob_url": old_url
    }
    d.client_factory.assert_not_called()
    d.blob.download.assert_not_called()
    d.blob.upload_json.assert_not_called()
    d.repo.succeed.assert_not_awaited()


def test_missing_document_is_an_explicit_state_failure(dependencies):
    d = dependencies
    d.repo.start_or_resume.side_effect = NormalizationStateError()
    with pytest.raises(NormalizationStateError):
        asyncio.run(activity.normalize_dmer_activity(INPUT))
    d.normalize.assert_not_called()


def test_concurrent_attempt_is_reported_as_busy_without_processing(dependencies):
    d = dependencies
    d.repo.start_or_resume.side_effect = NormalizationBusyError()
    with pytest.raises(NormalizationBusyError):
        asyncio.run(activity.normalize_dmer_activity(INPUT))
    d.normalize.assert_not_called()
    d.repo.fail.assert_not_awaited()


def test_named_section_extraction_layout_reaches_normalization(dependencies):
    d = dependencies
    fields = {"details_of_condition": "synthetic narrative"}
    d.blob.download.return_value = json.dumps(
        {"top_level": {}, "combined": {"fields": fields}}
    ).encode()
    asyncio.run(activity.normalize_dmer_activity(INPUT))
    assert d.normalize.call_args.args[1] == fields


def test_invalid_extraction_records_terminal_failure_before_model_or_upload(
    dependencies,
):
    d = dependencies
    d.blob.download.return_value = b'{"fields": ["not a mapping"]}'
    with pytest.raises(NormalizationValidationError):
        asyncio.run(activity.normalize_dmer_activity(INPUT))
    d.repo.fail.assert_awaited_once()
    d.normalize.assert_not_called()
    d.blob.upload_json.assert_not_called()
    d.repo.succeed.assert_not_awaited()


def test_validation_error_is_recorded_without_clinical_text_or_masking(dependencies):
    d = dependencies
    d.normalize.side_effect = NormalizationValidationError("private clinical content")
    d.repo.fail.side_effect = RuntimeError("database unavailable")
    with pytest.raises(NormalizationValidationError) as error:
        asyncio.run(activity.normalize_dmer_activity(INPUT))
    assert "private clinical content" not in str(error.value)
    assert "private clinical content" not in str(d.repo.fail.call_args)
    d.repo.succeed.assert_not_awaited()


def test_uncertain_commit_can_resume_persisted_success_without_reprocessing(
    dependencies,
):
    d = dependencies
    url = f"https://blob/normalized-dmer/{DOC_ID}/runs/42/normalized.json"

    async def commit_then_disconnect(*args, **kwargs):
        d.repo.start_or_resume.return_value = NormalizationRun(42, 2, url)
        raise ConnectionError("private SQL parameters")

    d.repo.succeed.side_effect = commit_then_disconnect
    with pytest.raises(activity.NormalizationActivityError) as error:
        asyncio.run(activity.normalize_dmer_activity(INPUT))
    assert "private SQL parameters" not in str(error.value)
    assert asyncio.run(activity.normalize_dmer_activity(INPUT)) == {
        "normalized_blob_url": url
    }
    assert d.normalize.call_count == 1
    assert d.blob.upload_json.call_count == 1


def test_functions_host_registers_activity_by_its_orchestration_name():
    import azure.durable_functions as df

    app = df.DFApp()
    activity.register(app)
    functions = app.get_functions()
    assert [function.get_function_name() for function in functions] == ["NormalizeDmer"]
    bindings = json.loads(functions[0].get_function_json())["bindings"]
    assert any(
        binding["type"] == "activityTrigger" and binding["name"] == "payload"
        for binding in bindings
    )
