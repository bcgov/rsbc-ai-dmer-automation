"""Resolve Driver Durable activity: find the document's driver and their open evaluation.

See docs/development/stages/03-document-orchestration.md ("Activity: Resolve
Driver"). First in the Document Orchestration: the Rule Engine counts the
document toward the driver's evaluation, so both must exist before it runs.

1. Mercury supplied a driver (Ingest set ``driver_key``): keep it. If the
   licence read off the page differs, record the mismatch.
2. Otherwise use the licence read off the page -- none readable goes to
   ``MANUAL_REVIEW`` (I-12).
3. Look the licence up with Mercury's ``GET by driver_licence``: no driver,
   or more than one, goes to ``MANUAL_REVIEW``; exactly one is upserted and
   attached (a page-read match is flagged for the Decision Gateway's
   ``MAP_DRIVER``, I-11).
4. Create or attach the driver's open ``driver_evaluation`` and set
   ``expected_document_count`` from the same Mercury response.

The licence number is never logged or returned (§9.2): the activity returns
``{"driver_key": ...}`` or ``{"manual_review": true, "reason": ...}``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import azure.durable_functions as df
from dmer_common.db.resolve_driver import (
    DRIVER_AMBIGUOUS,
    DRIVER_NOT_FOUND,
    LICENCE_LOOKUP,
    LICENCE_UNREADABLE,
    MERCURY_SUPPLIED,
    MercuryDriver,
    ResolveDriverBusyError,
    ResolveDriverNotReadyError,
    ResolveDriverStateError,
    resolve_driver_session,
)
from dmer_common.licence import normalize_licence
from dmer_common.mercury_client import MercuryClient
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine as _get_async_engine
from ._runtime import poison_as_result
from ._runtime import run_sync as _run_sync

_log = get_logger(__name__)

ACTIVITY_NAME = "ResolveDriver"


class ResolveDriverValidationError(ValueError):
    """Poison: bad input, or a Mercury driver record unusable as a driver."""


class ResolveDriverActivityError(RuntimeError):
    """Operational failure; the document orchestrator owns bounded retries."""


def _input(payload: dict) -> str:
    if not isinstance(payload, dict):
        raise ResolveDriverValidationError("Resolve Driver input must be an object")
    document_id = payload.get("document_id")
    try:
        if not isinstance(document_id, str):
            raise TypeError
        return str(UUID(document_id))
    except (ValueError, TypeError):
        raise ResolveDriverValidationError(
            "Resolve Driver input requires a document UUID"
        ) from None


def expected_document_count(
    mercury_driver: dict[str, Any],
    *,
    counted_types: frozenset[str],
    uncounted_statuses: frozenset[str],
) -> int:
    """How many of the driver's active documents the batch waits for:
    those of a counted type (default DMER) not in an uncounted status
    (default Rejected). Both lists come from configuration."""
    documents = mercury_driver.get("active_documents") or []
    return sum(
        1
        for d in documents
        if isinstance(d, dict)
        and str(d.get("document_type", "")).strip().lower() in counted_types
        and str(d.get("document_status", "")).strip().lower() not in uncounted_statuses
    )


def _mercury_driver(record: dict[str, Any]) -> MercuryDriver:
    licence = normalize_licence(str(record.get("licence_number") or ""))
    if licence is None:
        raise ResolveDriverValidationError("Mercury driver has no valid licence number")
    return MercuryDriver(
        licence_number=licence,
        mercury_driver_id=record.get("driver_id") or None,
        first_name=record.get("first_name") or None,
        last_name=record.get("last_name") or None,
    )


def _manual(reason: str) -> dict:
    return {"manual_review": True, "reason": reason}


async def resolve_driver_activity(payload: dict) -> dict:
    """Resolve the document's driver, or route it to manual review."""
    document_id = _input(payload)
    engine = None
    with document_id_context(document_id):
        _log.info("resolve driver: triggered")
        try:
            engine = await _get_async_engine()
            async with resolve_driver_session(engine, document_id) as repository:
                run = await repository.start_or_resume(now=datetime.now(UTC))
                if run.completed is not None:
                    _log.info("resolve driver: returning committed result")
                    done = run.completed
                    if done.manual_review_reason:
                        return _manual(done.manual_review_reason)
                    return {"driver_key": done.driver_key}
                try:
                    if run.driver_key and run.driver_licence:
                        licence, resolved_by = run.driver_licence, MERCURY_SUPPLIED
                        mismatch = (
                            bool(run.licence_read) and run.licence_read != licence
                        )
                    elif run.licence_read:
                        licence, resolved_by, mismatch = (
                            run.licence_read,
                            LICENCE_LOOKUP,
                            False,
                        )
                    else:
                        return await _route_to_review(
                            repository, run.run_id, LICENCE_UNREADABLE
                        )

                    client = MercuryClient()
                    drivers = await _run_sync(client.get_driver_by_licence, licence)
                    if not drivers:
                        return await _route_to_review(
                            repository, run.run_id, DRIVER_NOT_FOUND
                        )
                    if len(drivers) > 1:
                        return await _route_to_review(
                            repository, run.run_id, DRIVER_AMBIGUOUS
                        )

                    settings = client.driver_settings
                    expected = expected_document_count(
                        drivers[0],
                        counted_types=settings.counted_document_types,
                        uncounted_statuses=settings.uncounted_document_statuses,
                    )
                    driver_key, evaluation_id = await repository.resolve(
                        run.run_id,
                        mercury_driver=_mercury_driver(drivers[0]),
                        expected_document_count=expected,
                        resolved_by=resolved_by,
                        licence_mismatch=mismatch,
                        now=datetime.now(UTC),
                    )
                    _log.info(
                        "resolve driver: resolved",
                        extra={
                            "run_id": run.run_id,
                            "driver_key": driver_key,
                            "driver_evaluation_id": evaluation_id,
                            "resolved_by": resolved_by,
                            "licence_mismatch": mismatch,
                            "expected_document_count": expected,
                        },
                    )
                    if mismatch:
                        _log.warning(
                            "resolve driver: page licence differs from Mercury's driver"
                        )
                    return {"driver_key": driver_key}
                except Exception as exc:
                    try:
                        await repository.fail(
                            run.run_id,
                            ended_at=datetime.now(UTC),
                            error_code=type(exc).__name__,
                            error_detail="Driver resolution attempt failed; see error_code.",
                        )
                    except Exception:  # noqa: BLE001 - keep the original failure
                        _log.error("resolve driver: unable to record failed attempt")
                    raise
        except (
            ResolveDriverValidationError,
            ResolveDriverBusyError,
            ResolveDriverNotReadyError,
            ResolveDriverStateError,
        ):
            raise
        except Exception as exc:  # noqa: BLE001 - sanitize before Durable serialization
            _log.error(
                "resolve driver: operational failure",
                extra={"error_code": type(exc).__name__},
            )
            raise ResolveDriverActivityError(
                "Resolve Driver dependency or persistence failure"
            ) from None
        finally:
            if engine is not None:
                try:
                    await engine.dispose()
                except Exception:  # noqa: BLE001 - cleanup must not replace a result
                    _log.error("resolve driver: unable to dispose database engine")


async def _route_to_review(repository, run_id: int, reason: str) -> dict:
    await repository.manual_review(run_id, reason=reason, now=datetime.now(UTC))
    _log.warning("resolve driver: routed to manual review", extra={"reason": reason})
    return _manual(reason)


# Errors retrying can't fix: returned as a poison result for the orchestrator
# to route to MANUAL_REVIEW, instead of being retried.
POISON = (ResolveDriverValidationError, ResolveDriverStateError)


def register(app: df.DFApp) -> None:
    """Register the Resolve Driver activity on *app* (called once from
    function_app.py); kept out of import time like ``normalize.register``."""

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _resolve_driver(payload: dict) -> dict:
        return await poison_as_result(resolve_driver_activity(payload), POISON)
