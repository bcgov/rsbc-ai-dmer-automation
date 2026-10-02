"""Driver Lookup activity: the decision logic and failure contract.

The DB unit of work and the Mercury client are mocked; the repository itself is
tested against PostgreSQL in libs/dmer_common/tests/integration.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from dmer_common.config import MercuryDriverSettings
from dmer_common.db.driver_lookup import (
    DRIVER_AMBIGUOUS,
    DRIVER_NOT_FOUND,
    LICENCE_LOOKUP,
    LICENCE_UNREADABLE,
    MERCURY_SUPPLIED,
    DriverLookupBusyError,
    DriverLookupOutcome,
    DriverLookupRun,
)

import workflow_orchestrator.activities.driver_lookup as activity

DOC_ID = "123e4567-e89b-12d3-a456-426655440000"
DRIVER_KEY = "74f5a6ce-575a-4689-a553-99f50c24ca15"
LICENCE = "01234567"
OTHER_LICENCE = "07654321"
MERCURY_DRIVER = {
    "driver_id": "D1234",
    "first_name": "John",
    "middle_name": "",
    "last_name": "Doe",
    "licence_number": "1234567",
    "active_documents": [
        {"file_name": "a", "document_type": "DMER", "document_status": "Rejected"},
        {"file_name": "b", "document_type": "DMER", "document_status": "Uploaded"},
        {
            "file_name": "c",
            "document_type": "Test Report",
            "document_status": "Uploaded",
        },
    ],
    "case": {"case_id": "C1234"},
}
SETTINGS = MercuryDriverSettings(
    base_url="https://mercury/drivers",
    api_key="key",  # pragma: allowlist secret
    counted_document_types=frozenset({"dmer"}),
    uncounted_document_statuses=frozenset({"rejected"}),
)


def _run_for(
    *, driver_key=None, driver_licence=None, licence_read=None, completed=None
):
    return DriverLookupRun(5, 1, driver_key, driver_licence, licence_read, completed)


@pytest.fixture
def dependencies(monkeypatch):
    repository = SimpleNamespace(
        start_or_resume=AsyncMock(return_value=_run_for(licence_read=LICENCE)),
        attach_driver=AsyncMock(return_value=(DRIVER_KEY, "eval-1")),
        manual_review=AsyncMock(),
        fail=AsyncMock(),
    )
    engine = SimpleNamespace(dispose=AsyncMock())
    client = Mock()
    client.get_driver_by_licence.return_value = [MERCURY_DRIVER]
    client.driver_settings = SETTINGS

    @asynccontextmanager
    async def session(received_engine, document_id):
        assert received_engine is engine and document_id == DOC_ID
        yield repository

    monkeypatch.setattr(activity, "driver_lookup_session", session)
    monkeypatch.setattr(activity, "_get_async_engine", AsyncMock(return_value=engine))
    monkeypatch.setattr(activity, "MercuryClient", lambda: client)
    return SimpleNamespace(repo=repository, engine=engine, client=client)


def _run():
    return asyncio.run(activity.driver_lookup_activity({"document_id": DOC_ID}))


def test_page_licence_found_once_is_attached_and_flagged_as_a_lookup(dependencies):
    d = dependencies
    assert _run() == {"driver_key": DRIVER_KEY}
    d.client.get_driver_by_licence.assert_called_once_with(LICENCE)
    kwargs = d.repo.attach_driver.call_args.kwargs
    assert kwargs["resolved_by"] == LICENCE_LOOKUP
    assert kwargs["licence_mismatch"] is False
    assert kwargs["mercury_driver"].licence_number == LICENCE  # canonical 8 digits
    assert kwargs["mercury_driver"].mercury_driver_id == "D1234"
    d.engine.dispose.assert_awaited_once()


def test_expected_count_is_counted_types_not_in_uncounted_statuses(dependencies):
    _run()
    # DMER/Rejected and Test Report/Uploaded are not counted.
    assert (
        dependencies.repo.attach_driver.call_args.kwargs["expected_document_count"] == 1
    )


@pytest.mark.parametrize(
    "types,statuses,expected",
    [
        ({"dmer"}, set(), 2),
        ({"dmer", "test report"}, {"rejected"}, 2),
        ({"dmer", "test report"}, set(), 3),
    ],
)
def test_expected_count_follows_configuration(types, statuses, expected):
    assert (
        activity.expected_document_count(
            MERCURY_DRIVER,
            counted_types=frozenset(types),
            uncounted_statuses=frozenset(statuses),
        )
        == expected
    )


def test_mercury_supplied_driver_is_kept_and_a_different_page_licence_is_recorded(
    dependencies,
):
    d = dependencies
    d.repo.start_or_resume.return_value = _run_for(
        driver_key=DRIVER_KEY, driver_licence=LICENCE, licence_read=OTHER_LICENCE
    )
    assert _run() == {"driver_key": DRIVER_KEY}
    d.client.get_driver_by_licence.assert_called_once_with(
        LICENCE
    )  # Mercury's, not the page's
    kwargs = d.repo.attach_driver.call_args.kwargs
    assert kwargs["resolved_by"] == MERCURY_SUPPLIED
    assert kwargs["licence_mismatch"] is True


@pytest.mark.parametrize("licence_read", [LICENCE, None])
def test_mercury_supplied_driver_without_a_different_page_licence_is_no_mismatch(
    dependencies, licence_read
):
    d = dependencies
    d.repo.start_or_resume.return_value = _run_for(
        driver_key=DRIVER_KEY, driver_licence=LICENCE, licence_read=licence_read
    )
    _run()
    assert d.repo.attach_driver.call_args.kwargs["licence_mismatch"] is False


@pytest.mark.parametrize(
    "run,drivers,reason",
    [
        (_run_for(), [MERCURY_DRIVER], LICENCE_UNREADABLE),
        (_run_for(licence_read=LICENCE), [], DRIVER_NOT_FOUND),
        (
            _run_for(licence_read=LICENCE),
            [MERCURY_DRIVER, MERCURY_DRIVER],
            DRIVER_AMBIGUOUS,
        ),
    ],
)
def test_unresolvable_documents_go_to_manual_review(dependencies, run, drivers, reason):
    d = dependencies
    d.repo.start_or_resume.return_value = run
    d.client.get_driver_by_licence.return_value = drivers
    assert _run() == {"manual_review": True, "reason": reason}
    assert d.repo.manual_review.call_args.kwargs["reason"] == reason
    d.repo.attach_driver.assert_not_awaited()
    if reason == LICENCE_UNREADABLE:
        d.client.get_driver_by_licence.assert_not_called()


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (DriverLookupOutcome(DRIVER_KEY), {"driver_key": DRIVER_KEY}),
        (
            DriverLookupOutcome(None, DRIVER_NOT_FOUND),
            {"manual_review": True, "reason": DRIVER_NOT_FOUND},
        ),
    ],
)
def test_a_committed_result_is_returned_without_calling_mercury(
    dependencies, outcome, expected
):
    d = dependencies
    d.repo.start_or_resume.return_value = _run_for(completed=outcome)
    assert _run() == expected
    d.client.get_driver_by_licence.assert_not_called()


def test_mercury_failure_is_recorded_and_sanitized(dependencies):
    d = dependencies
    d.client.get_driver_by_licence.side_effect = ConnectionError(f"private {LICENCE}")
    with pytest.raises(activity.DriverLookupActivityError) as error:
        _run()
    assert LICENCE not in str(error.value)
    d.repo.fail.assert_awaited_once()
    d.repo.attach_driver.assert_not_awaited()


def test_a_mercury_driver_without_a_valid_licence_is_poison(dependencies):
    d = dependencies
    d.client.get_driver_by_licence.return_value = [
        {**MERCURY_DRIVER, "licence_number": "X"}
    ]
    with pytest.raises(activity.DriverLookupValidationError):
        _run()
    d.repo.fail.assert_awaited_once()


def test_the_licence_is_never_in_the_result_or_logs(dependencies, monkeypatch):
    log = Mock()
    monkeypatch.setattr(activity, "_log", log)
    dependencies.repo.start_or_resume.return_value = _run_for(
        driver_key=DRIVER_KEY, driver_licence=LICENCE, licence_read=OTHER_LICENCE
    )
    result = _run()
    logged = repr(log.mock_calls) + repr(result)
    for value in (LICENCE, OTHER_LICENCE, "1234567"):
        assert value not in logged


def test_busy_document_passes_through(dependencies):
    dependencies.repo.start_or_resume.side_effect = DriverLookupBusyError()
    with pytest.raises(DriverLookupBusyError):
        _run()


@pytest.mark.parametrize("payload", [{}, {"document_id": "nope"}, "not an object"])
def test_bad_input_is_poison_before_any_io(dependencies, payload):
    with pytest.raises(activity.DriverLookupValidationError):
        asyncio.run(activity.driver_lookup_activity(payload))
    dependencies.repo.start_or_resume.assert_not_awaited()


def test_functions_host_registers_activity_by_its_orchestration_name():
    import azure.durable_functions as df

    app = df.DFApp()
    activity.register(app)
    functions = app.get_functions()
    assert [f.get_function_name() for f in functions] == ["DriverLookup"]
    bindings = json.loads(functions[0].get_function_json())["bindings"]
    assert any(
        b["type"] == "activityTrigger" and b["name"] == "payload" for b in bindings
    )
