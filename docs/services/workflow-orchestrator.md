
# workflow-orchestrator

**Azure compute:** Durable Functions (orchestrator + activities)

## Responsibilities

- Triggered by `extracted-dmer-queue`.
- Orchestrates, as durable activities: metadata loading, duplicate validation (against
  `document_hash`/`duplicate_checks`), case creation/update in Mercury, normalization
  invocation (`normalizer-service`), rule evaluation invocation (`rule-engine`), Mercury
  system update, completion marking, and failure routing/compensation.
- Owns retry policies (activity-level) and the overall processing state machine.
- Publishes lifecycle events (`completed` / `failed`) to the `dmer-lifecycle-events` topic
  for `post-processing` and `audit-service` to consume.

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/workflow-orchestrator/` for the required keys.
