# Azure Functions

Source: architecture doc §3.2, §4. Bicep module: `infrastructure/bicep/modules/compute/function-app.bicep`.

## Role in the pipeline

Every stage except [Extraction](../stages/02-extraction.md) (the one Container App) runs as an
Azure Function:

| Function App (proposed) | Stage(s) | Trigger(s) |
|---|---|---|
| Ingest | [01 — Ingest](../stages/01-ingest.md) | Timer (Page Poller), Service Bus queue (Ingest Function), HTTP (Webhook Listener, optional) |
| Document orchestration | [03 — Document Orchestration](../stages/03-document-orchestration.md), [04 — Normalize](../stages/04-activity-normalize.md), [05 — Rule Engine](../stages/05-activity-rule-engine.md) | Service Bus queue (starts orchestration), Durable activities |
| Driver orchestration | [06 — Driver Orchestration](../stages/06-driver-orchestration.md), [07 — Decision Gateway](../stages/07-decision-gateway.md), [08 — Post-Processing](../stages/08-post-processing.md) | Session-enabled Service Bus queue (starts orchestration), Durable activities |
| Reliability | [09 — Reliability Components](../stages/09-reliability-components.md), [10 — DLQ Drain](../stages/10-dlq-drain.md) | Timer ×3 |

The exact Function App boundaries (how many separate apps, which functions share a host) are a
repository-structure decision — see each stage doc's "Alignment gaps" section and the top-level
`../README.md#open-questions--decisions-required`.

## Plan

**Flex Consumption or Premium plan**, required for:
- VNet integration (to reach private-endpoint PostgreSQL, Service Bus, Blob, Document Intelligence).
- No cold start on the pipeline's latency-sensitive coordination points (Document/Driver
  Orchestration).

Consumption plan is not viable anywhere in this pipeline because every Function App needs VNet
integration for at least PostgreSQL access.

## Durable Functions specifics

The Document and Driver orchestrators are Durable Functions. Read
[Document Orchestration §How Durable Functions actually move work](../stages/03-document-orchestration.md#how-durable-functions-actually-move-work)
before writing orchestrator code — determinism rules, activity input/output size limits, and when
to use a plain activity vs. the async HTTP pattern vs. external events all apply here. Key points
repeated because they're easy to violate accidentally:

- Orchestrator functions must be deterministic — no I/O, no `datetime.now()`, no random/GUID
  generation outside `context.current_utc_datetime`/`context.new_guid()`.
- Pass IDs and blob URLs between activities, never extracted/normalized JSON content.
- `instanceId` is the idempotency boundary for orchestration starts — `document_guid` for Document
  Orchestration, `driver_key` for Driver Orchestration.
- Task hub storage (the Durable extension's own history/control-queue storage account) is separate
  from `dmer_common`'s Blob/Service Bus clients — don't conflate the two.

## Session-enabled triggers

The `driver-decision` queue requires session support (`SessionId = driver_key`) — see
`../message-contracts.md`. Confirm the Python Durable Functions Service Bus session trigger binding
supports this before Phase 4 build; if it doesn't cleanly support sessions, the Postgres advisory
lock alternative (noted in [Driver Orchestration](../stages/06-driver-orchestration.md#the-mechanism))
becomes the fallback.

## Configuration / environment variables

Loaded via `dmer_common.config` (App Configuration + Key Vault references resolved to env vars at
runtime — see `azure-key-vault.md`). Each Function App's `local.settings.json` (local dev only,
gitignored) mirrors the same keys documented per stage.

## Authentication / identity

One user-assigned managed identity per Function App (least-privilege RBAC only — no
`Owner`/`Contributor`). `DefaultAzureCredential` via `libs/dmer_common/src/dmer_common/auth/`
(currently a docstring-only stub — needs the actual credential wrapper implemented).

## Logging / auditing

`dmer_common.telemetry.get_logger()` — structured JSON, document-ID-bound, PII-redacting (see
`azure-monitor.md`). Application Insights via the Functions host's built-in integration plus this
structured logger.

## Dependencies

`azure-functions`, `azure-functions-durable` (Python Durable Functions), `dmer_common` (shared
library — messaging, storage, retry, telemetry, config). None of the current
`services/*/requirements.txt` files have these pinned yet (all are placeholder comments).

## Implementation considerations for Claude Code

- Every current `services/*/function_app.py` (`intake-processor`, `workflow-orchestrator`,
  `rule-engine`, `post-processing`) uses the Python v2 programming model (`func.FunctionApp()`,
  decorator-based triggers) — keep using v2 for consistency; it's already the established pattern
  even though no triggers are registered yet.
- `host.json` exists per Function App placeholder already (`intake-processor`, `workflow-orchestrator`,
  `rule-engine`, `post-processing`) — check its `extensions.durableTask` section is configured before
  adding orchestrator code to any app that needs it.
