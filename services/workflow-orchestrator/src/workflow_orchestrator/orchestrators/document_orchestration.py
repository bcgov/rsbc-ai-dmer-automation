"""Document Orchestration: the per-document Durable orchestrator.

See docs/development/stages/03-document-orchestration.md. Deterministic only
per that doc's replay rules -- no I/O, no environment reads, no
``datetime.now()``/random values here; all of that lives in the activities
this calls.

Runs ``ResolveDriver``, ``NormalizeDmer`` and ``RunRuleEngine`` in sequence.
A document Resolve Driver routes to manual review ends there -- with no
driver it can never join a driver batch. Not built yet: the driver-decision
publish at the end.
"""

from __future__ import annotations

import azure.durable_functions as df

ORCHESTRATION_NAME = "DocumentOrchestration"


def document_orchestration(context: df.DurableOrchestrationContext):
    """ResolveDriver -> NormalizeDmer -> RunRuleEngine -> (driver-decision, not yet built).

    Input: ``{"document_id": ..., "driver_key": ..., "extracted_blob_url": ...}``
    (the Document Orchestration trigger contract). Resolve Driver reads the
    document's driver from the database, where Ingest recorded Mercury's.
    Activities get ids and blob URLs only, never document content.
    """
    trigger_input = context.get_input()
    document_id = trigger_input["document_id"]
    driver = yield context.call_activity("ResolveDriver", {"document_id": document_id})
    if driver.get("manual_review"):
        return driver
    normalize_result = yield context.call_activity(
        "NormalizeDmer",
        {
            "document_id": document_id,
            "extracted_blob_url": trigger_input["extracted_blob_url"],
        },
    )
    rule_result = yield context.call_activity(
        "RunRuleEngine",
        {
            "document_id": document_id,
            "normalized_blob_url": normalize_result["normalized_blob_url"],
        },
    )
    # TODO: publish to driver-decision with SessionId = driver["driver_key"]
    # (03-document-orchestration.md step 5).
    return {**rule_result, "driver_key": driver["driver_key"]}


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
