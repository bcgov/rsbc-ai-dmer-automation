
# Operations Runbook

## Daily checks

- Service Bus queue/DLQ depths (`dmer-ingest`, `dmer-raw`, `dmer-extracted`)
- Application Insights failure rate per service
- PostgreSQL Flexible Server CPU/storage/connection count

## Common operational tasks

| Task | Procedure |
|---|---|
| Reprocess a dead-lettered message | See `troubleshooting.md#dead-lettered-messages` |
| Rotate a rule version | Run the rule engine tests against the new file, publish `rules/versions/<version>/rules.json`, update `rules/active/rules.json`, and insert its `rules_version` row (`version` per [data-model.md](../development/data-model.md#rules_version), the file's sha256 as `checksum`) — the pipeline never registers it; an unregistered active file stops evaluation. Monitor `rule_evaluation.rules_version` for the new version. During development, a correction keeps the version: upload it and update that row's `checksum` instead |
| Rotate Managed Identity credentials | Not applicable — Managed Identity has no rotatable secret |
| Scale Container Apps | Adjust KEDA scale rule parameters in `deployment/<env>/parameters.json`, redeploy |

See `incident-response.md` for P1/P2 handling.
