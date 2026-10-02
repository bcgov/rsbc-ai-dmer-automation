"""Document Orchestration: the per-document Durable orchestrator.

See docs/development/stages/03-document-orchestration.md. Deterministic only
per that doc's replay rules -- no I/O, no environment reads, no
``datetime.now()``/random values here; all of that lives in the activities
this calls.

Runs ``NormalizeDmer`` then ``RunRuleEngine``. Not built yet: Resolve Driver
(before Normalize; blocked on the Mercury driver-licence API and the meaning
of an "open" driver evaluation) and the driver-decision publish at the end
(it needs the resolved ``driver_key``).
"""

from __future__ import annotations

import azure.durable_functions as df

ORCHESTRATION_NAME = "DocumentOrchestration"


def document_orchestration(context: df.DurableOrchestrationContext):
    """NormalizeDmer -> RunRuleEngine -> (driver-decision, not yet built).

    Input: ``{"document_id": ..., "driver_key": ..., "extracted_blob_url": ...}``
    (the Document Orchestration trigger contract). ``driver_key`` isn't used
    yet but is carried in the input contract for the driver-decision publish.
    Activities get ids and blob URLs only, never document content.
    """
    trigger_input = context.get_input()
    document_id = trigger_input["document_id"]
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
    # TODO: publish to driver-decision with SessionId = driver_key once Resolve
    # Driver exists (03-document-orchestration.md step 5).
    return rule_result


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
