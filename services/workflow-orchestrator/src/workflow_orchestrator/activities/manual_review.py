"""Route To Manual Review Durable activity.

See docs/development/stages/03-document-orchestration.md ("Failure handling"):
a document an activity could not process -- a poison result, or retries
exhausted -- is routed to ``MANUAL_REVIEW`` rather than letting the
orchestration instance fail silently, with a ``processing_error`` row: the
stage, the failure category and a reason code (``<activity>:<error>``).

A document that already has its driver (it failed after Driver Lookup) is
counted into the driver's open batch by the same write, and its driver is
then signalled on ``driver-decision``: the batch can be decided without it
instead of waiting for it forever. The signal's ``message_id`` is distinct
from Signal Driver's for the same document, so duplicate detection never
drops it. A failed publish is logged, not raised: the document is already
counted, so a missed signal only delays the decision to the driver's next
signal (or the Reconciliation Sweeper).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

import azure.durable_functions as df
from dmer_common.db.document_routing import load_signal_state, route_to_manual_review
from dmer_common.db.processing_error import FailureCategory
from dmer_common.dto import (
    DRIVER_DECISION_EVENT,
    DriverDecisionMessage,
    event_message_id,
)
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine as _get_async_engine
from ._runtime import run_sync as _run_sync
from .signal_driver import publish_driver_decision

_log = get_logger(__name__)

ACTIVITY_NAME = "RouteToManualReview"

# A reason is a code ("NormalizeDmer:RETRIES_EXHAUSTED"), never free text.
_REASON_RE = re.compile(r"[A-Za-z0-9_.:-]{1,200}")
_STAGES = frozenset({"DRIVER_LOOKUP", "NORMALIZE", "RULES", "DECISION"})
# processing_error.message: a fixed description per category, never content.
_MESSAGES = {
    FailureCategory.PERMANENT_BUSINESS: "The activity reported an error retrying can't fix.",
    FailureCategory.UNKNOWN: "The activity failed on every retry; see its logs and stage run.",
    FailureCategory.PROCESSING: "A dependency failed past the retry budget.",
    FailureCategory.TRANSIENT: "A transient infrastructure failure outlasted the retries.",
}


class ManualReviewValidationError(ValueError):
    """Bad input."""


async def route_to_manual_review_activity(payload: dict) -> dict:
    """Route the document to MANUAL_REVIEW; ``{"routed": False}`` when it is
    already terminal (MANUAL_REVIEW keeps its first reason)."""
    if not isinstance(payload, dict):
        raise ManualReviewValidationError("Manual review input must be an object")
    try:
        document_id = str(UUID(str(payload.get("document_id"))))
    except ValueError:
        raise ManualReviewValidationError(
            "Manual review input requires a document UUID"
        ) from None
    reason = payload.get("reason")
    if not isinstance(reason, str) or not _REASON_RE.fullmatch(reason):
        raise ManualReviewValidationError("Manual review input requires a reason code")
    stage = payload.get("stage")
    if stage not in _STAGES:
        raise ManualReviewValidationError(
            "Manual review input requires a pipeline stage"
        )
    try:
        category = FailureCategory(payload.get("category"))
    except ValueError:
        raise ManualReviewValidationError(
            "Manual review input requires a failure category"
        ) from None

    blob_url = payload.get("blob_url")
    if blob_url is not None and not isinstance(blob_url, str):
        raise ManualReviewValidationError("Manual review blob_url must be a string")

    with document_id_context(document_id):
        engine = await _get_async_engine()
        try:
            routed = await route_to_manual_review(
                engine,
                document_id,
                stage=stage,
                category=category,
                reason_code=reason,
                message=_MESSAGES[category],
                now=datetime.now(UTC),
            )
            _log.warning(
                (
                    "routed to manual review"
                    if routed
                    else "manual review: already terminal"
                ),
                extra={
                    "reason": reason,
                    "stage": stage,
                    "failure_category": category.value,
                },
            )
            signalled = await _signal_driver(engine, document_id, blob_url)
        finally:
            await engine.dispose()
        return {"routed": routed, "driver_signalled": signalled}


async def _signal_driver(engine, document_id: str, blob_url: str | None) -> bool:
    """Signal the document's driver if it is in MANUAL_REVIEW with a driver
    (counted into the batch by the routing write). Also on a retried call,
    so a signal lost to a crash is re-sent.

    Never raises -- any failure (reading the document, building or publishing
    the message) is logged and reported as not signalled: the routing write
    is already committed, so a missed signal only delays the decision.
    """
    try:
        state = await load_signal_state(engine, document_id)
        if (
            state is None
            or state.pipeline_status != "MANUAL_REVIEW"
            or not state.driver_key
        ):
            return False
        if not blob_url:
            _log.warning("manual review: no blob URL; driver not signalled")
            return False
        message = DriverDecisionMessage(
            message_id=event_message_id(
                DRIVER_DECISION_EVENT, f"{document_id}:manual-review"
            ),
            document_id=document_id,
            document_guid=state.document_guid,
            driver_key=state.driver_key,
            blob_url=blob_url,
            enqueued_at=datetime.now(UTC),
        )
        await _run_sync(publish_driver_decision, message)
    except Exception as exc:  # noqa: BLE001 - routed already; see the docstring
        _log.error(
            "manual review: driver signal failed",
            extra={"error_code": type(exc).__name__},
        )
        return False
    _log.info(
        "manual review: driver signalled",
        extra={"message_id": message.message_id, "driver_key": state.driver_key},
    )
    return True


def register(app: df.DFApp) -> None:
    """Register the Route To Manual Review activity on *app*."""

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _route_to_manual_review(payload: dict) -> dict:
        return await route_to_manual_review_activity(payload)
