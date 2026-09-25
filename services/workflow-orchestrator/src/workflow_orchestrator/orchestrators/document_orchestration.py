"""Document Orchestration: the per-document Durable orchestrator.

See docs/development/stages/03-document-orchestration.md. Deterministic only
per that doc's replay rules -- no I/O, no environment reads, no
``datetime.now()``/random values here; all of that lives in the activities
this calls.

Scoped down from the doc's full flow for now: calls ``NormalizeDmer`` only.
``RunRuleEngine`` (docs/development/stages/05-activity-rule-engine.md) does
not exist yet, so neither does the driver-decision publish that follows it
in the documented flow (03-document-orchestration.md#orchestration-flow) --
that hand-off needs Rule Engine's outcome, so there's nothing to publish
until it's built.
"""

from __future__ import annotations

import azure.durable_functions as df

ORCHESTRATION_NAME = "DocumentOrchestration"


def document_orchestration(context: df.DurableOrchestrationContext):
    """NormalizeDmer -> (RunRuleEngine, not yet built) -> (driver-decision, not yet built).

    Input: ``{"document_id": ..., "driver_key": ..., "extracted_blob_url": ...}``
    (the Document Orchestration trigger contract). ``driver_key`` isn't used
    by Normalize but is carried through so it's already part of this
    orchestrator's input contract once RunRuleEngine/driver-decision need it
    -- avoids a second breaking change to the starter that calls this.
    """
    trigger_input = context.get_input()
    normalize_result = yield context.call_activity(
        "NormalizeDmer",
        {
            "document_id": trigger_input["document_id"],
            "extracted_blob_url": trigger_input["extracted_blob_url"],
        },
    )
    # TODO: yield context.call_activity("RunRuleEngine", {...}) once that
    # activity exists, then publish to driver-decision with
    # SessionId = driver_key (03-document-orchestration.md step 4).
    # Returning Normalize's result in the meantime so the orchestration's
    # output is observable via get_status() rather than null.
    return normalize_result


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
