"""Route To Manual Review Durable activity.

See docs/development/stages/03-document-orchestration.md ("Failure handling"):
a document an activity could not process -- a poison result, or retries
exhausted -- is routed to ``MANUAL_REVIEW`` rather than letting the
orchestration instance fail silently, with a ``processing_error`` row: the
stage, the failure category and a reason code (``<activity>:<error>``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

import azure.durable_functions as df
from dmer_common.db.document_routing import route_to_manual_review
from dmer_common.db.processing_error import FailureCategory
from dmer_common.telemetry import document_id_context, get_logger

from ._runtime import get_async_engine as _get_async_engine

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
        finally:
            await engine.dispose()
        _log.warning(
            "routed to manual review" if routed else "manual review: already terminal",
            extra={
                "reason": reason,
                "stage": stage,
                "failure_category": category.value,
            },
        )
        return {"routed": routed}


def register(app: df.DFApp) -> None:
    """Register the Route To Manual Review activity on *app*."""

    @app.function_name(name=ACTIVITY_NAME)
    @app.activity_trigger(input_name="payload")
    async def _route_to_manual_review(payload: dict) -> dict:
        return await route_to_manual_review_activity(payload)
