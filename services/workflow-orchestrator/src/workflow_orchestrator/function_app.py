# workflow-orchestrator — Azure Functions (Python v2 model, Durable Functions)
#
# Document Orchestration stage (docs/development/stages/03-document-orchestration.md)
# + its activities (Normalize, Rule Engine) -- see that doc's
# "Alignment gaps" section for why this is scoped down from the *original*
# architecture's single do-everything orchestration.
#
# DFApp exposes orchestration/activity/client bindings alongside the normal
# Functions bindings. The root function_app.py imports this app for host
# discovery.
#
# Registered here: the dmer-extracted queue trigger that starts one
# DocumentOrchestration instance per document (this module), the
# DocumentOrchestration orchestrator (orchestrators/document_orchestration.py),
# and the Normalize and Rule Engine activities (activities/normalize.py,
# activities/rule_engine.py).

from __future__ import annotations

import json
from urllib.parse import urlparse
from uuid import UUID

import azure.durable_functions as df
import azure.functions as func
from azure.durable_functions.models.OrchestrationRuntimeStatus import (
    OrchestrationRuntimeStatus,
)
from dmer_common.telemetry import document_id_context, get_logger

from .activities import normalize, rule_engine
from .orchestrators import document_orchestration

_log = get_logger(__name__)

app = df.DFApp()

normalize.register(app)
rule_engine.register(app)
document_orchestration.register(app)

# An instance in any of these states is already in flight or already done --
# starting a new one would duplicate Normalize's and Rule Engine's work. See
# 03-document-orchestration.md's idempotency requirements: the dmer-extracted
# trigger must not start a second
# orchestration instance for a document_guid that already has one.
_SKIP_START_STATUSES = frozenset(
    {
        OrchestrationRuntimeStatus.Pending,
        OrchestrationRuntimeStatus.Running,
        OrchestrationRuntimeStatus.ContinuedAsNew,
        OrchestrationRuntimeStatus.Suspended,
        OrchestrationRuntimeStatus.Completed,
    }
)


def _parse_trigger_message(body: bytes) -> tuple[str, dict]:
    """Parses one dmer-extracted envelope into (document_guid, orchestration_input).

    Reads the wire envelope's camelCase fields (schemaVersion/documentId/
    documentGuid/driverKey/blobUrl -- see ../../../../docs/development/message-contracts.md)
    and returns the snake_case shape document_orchestration.py's input
    contract expects. Raises on anything malformed -- the caller lets that
    propagate so the message is redelivered/eventually dead-lettered, same
    documented gap as intake-processor's dmer_ingest (explicit immediate
    dead-lettering for known-poison input isn't implemented in this pass).
    """
    envelope = json.loads(body.decode("utf-8"))
    document_id = str(UUID(envelope["documentId"]))
    document_guid = str(UUID(envelope["documentGuid"]))
    blob_url = envelope.get("blobUrl")
    parsed = urlparse(blob_url or "")
    if parsed.scheme not in ("https", "http") or not parsed.netloc:
        raise ValueError("dmer-extracted message requires a blobUrl")
    orchestration_input = {
        "document_id": document_id,
        "driver_key": envelope.get("driverKey"),
        "extracted_blob_url": blob_url,
    }
    return document_guid, orchestration_input


async def start_document_orchestration(
    msg: func.ServiceBusMessage, client: df.DurableOrchestrationClient
) -> None:
    """Starts one DocumentOrchestration instance per document_guid.

    Bare function (no Functions-host decorators) so it's directly callable
    in a unit test with a plain fake client -- ``@durable_client_input``
    wraps the *registered* function with middleware that expects to build
    the rich client itself from raw binding data, so that decorated version
    can't be called directly with an already-constructed client. Same split
    activities/normalize.py uses between its testable function and
    ``register()``.

    instance_id = document_guid (not document_id) so a redelivered
    dmer-extracted message -- or one redelivered after DLQ Drain replays it
    -- targets the same orchestration instance rather than starting a
    second one; see 03-document-orchestration.md#idempotency-requirements.
    """
    document_guid, orchestration_input = _parse_trigger_message(msg.get_body())
    with document_id_context(orchestration_input["document_id"]):
        existing = await client.get_status(document_guid)
        if existing is not None and existing.runtime_status in _SKIP_START_STATUSES:
            _log.info(
                "start_document_orchestration: instance already exists; no-op",
                extra={"runtime_status": str(existing.runtime_status)},
            )
            return
        instance_id = await client.start_new(
            document_orchestration.ORCHESTRATION_NAME,
            instance_id=document_guid,
            client_input=orchestration_input,
        )
        _log.info(
            "start_document_orchestration: started", extra={"instance_id": instance_id}
        )


@app.function_name(name="StartDocumentOrchestration")
@app.service_bus_queue_trigger(
    arg_name="msg",
    queue_name="%DMER_EXTRACTED_QUEUE%",
    connection="ServiceBusConnection",
)
@app.durable_client_input(client_name="client")
async def _start_document_orchestration(
    msg: func.ServiceBusMessage, client: df.DurableOrchestrationClient
) -> None:
    await start_document_orchestration(msg, client)
