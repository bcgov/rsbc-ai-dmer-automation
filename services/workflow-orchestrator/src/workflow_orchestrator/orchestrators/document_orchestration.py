"""Document Orchestration: the per-document Durable orchestrator.

See docs/development/stages/03-document-orchestration.md. Deterministic only
per that doc's replay rules -- no I/O, no environment reads, no
``datetime.now()``/random values here; all of that lives in the activities
this calls (the retry settings arrive in the orchestration input, read from
the environment by the starter).

DriverLookup -> NormalizeDmer -> RunRuleEngine -> SignalDriver.

Failure handling: every activity runs with the retry policy, so a transient
failure is retried with backoff. A document that still can't be processed --
retries exhausted, or a *poison* result (an error retrying can't fix, which
the activity returns instead of raising) -- is routed to ``MANUAL_REVIEW`` by
``RouteToManualReview`` with the reason ``<activity>:<error>``, and the
orchestration ends. Driver Lookup routes its own unresolvable documents.
"""

from __future__ import annotations

import azure.durable_functions as df

ORCHESTRATION_NAME = "DocumentOrchestration"

# Used when the orchestration input carries no retry settings.
DEFAULT_RETRY = {"first_retry_interval_ms": 30_000, "max_attempts": 3}
RETRIES_EXHAUSTED = "RETRIES_EXHAUSTED"


def _retry_options(trigger_input: dict) -> df.RetryOptions:
    settings = {**DEFAULT_RETRY, **(trigger_input.get("retry") or {})}
    return df.RetryOptions(
        int(settings["first_retry_interval_ms"]), int(settings["max_attempts"])
    )


def _call(context, name: str, payload: dict, retry: df.RetryOptions):
    """Run one activity with the retry policy; returns ``(result, failure_reason)``."""
    try:
        result = yield context.call_activity_with_retry(name, retry, payload)
    # Retries exhausted; the cause is in the activity's logs and stage run.
    except Exception:  # noqa: BLE001
        return None, f"{name}:{RETRIES_EXHAUSTED}"
    if isinstance(result, dict) and result.get("poison"):
        return None, f"{name}:{result.get('error_code') or 'Poison'}"
    return result, None


def _manual_review(context, document_id: str, reason: str, retry: df.RetryOptions):
    yield context.call_activity_with_retry(
        "RouteToManualReview", retry, {"document_id": document_id, "reason": reason}
    )
    return {"manual_review": True, "reason": reason}


def document_orchestration(context: df.DurableOrchestrationContext):
    """DriverLookup -> NormalizeDmer -> RunRuleEngine -> SignalDriver.

    Input: ``{"document_id", "driver_key", "extracted_blob_url", "retry"}``
    (the Document Orchestration trigger contract; ``retry`` is
    ``{"first_retry_interval_ms", "max_attempts"}``). Driver Lookup reads the
    document's driver from the database, where Ingest recorded Mercury's.
    Activities get ids and blob URLs only, never document content.
    """
    trigger_input = context.get_input()
    document_id = trigger_input["document_id"]
    retry = _retry_options(trigger_input)

    driver, failure = yield from _call(
        context, "DriverLookup", {"document_id": document_id}, retry
    )
    if failure:
        return (yield from _manual_review(context, document_id, failure, retry))
    if driver.get("manual_review"):
        return driver  # Driver Lookup already routed it

    normalized, failure = yield from _call(
        context,
        "NormalizeDmer",
        {
            "document_id": document_id,
            "extracted_blob_url": trigger_input["extracted_blob_url"],
        },
        retry,
    )
    if failure:
        return (yield from _manual_review(context, document_id, failure, retry))
    normalized_blob_url = normalized["normalized_blob_url"]

    rules, failure = yield from _call(
        context,
        "RunRuleEngine",
        {"document_id": document_id, "normalized_blob_url": normalized_blob_url},
        retry,
    )
    if failure:
        return (yield from _manual_review(context, document_id, failure, retry))

    signal, failure = yield from _call(
        context,
        "SignalDriver",
        {"document_id": document_id, "normalized_blob_url": normalized_blob_url},
        retry,
    )
    if failure:
        return (yield from _manual_review(context, document_id, failure, retry))

    return {
        **rules,
        "driver_key": driver["driver_key"],
        "driver_signalled": bool(signal.get("published")),
    }


def register(app: df.DFApp) -> None:
    """Register the Document Orchestration orchestrator on *app* -- called
    once from function_app.py. Kept separate from the module-level function
    (matching activities/normalize.py's convention) so ``document_orchestration``
    has no import-time dependency on a live DFApp instance and can be driven
    directly as a generator in a unit test.
    """

    @app.function_name(name=ORCHESTRATION_NAME)
    @app.orchestration_trigger(context_name="context")
    def _document_orchestration(context: df.DurableOrchestrationContext):
        return document_orchestration(context)
