
# rule-engine

**Azure compute:** Azure Functions (HTTP-triggered activity)

## Responsibilities

- Invoked by `workflow-orchestrator` as a durable activity.
- Loads the active `rules.json` ruleset from Blob Storage (`rules/active/rules.json`)
  using GoRules/Zen.
- Evaluates the normalized ontology against the ruleset.
- Produces a decision (next action) and structured reason codes.
- Persists the evaluation input/output snapshot and rule version to PostgreSQL
  (`rule_execution`).

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/rule-engine/` for the required keys.
