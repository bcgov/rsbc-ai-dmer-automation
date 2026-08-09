
# Operations Runbook

## Daily checks

- Service Bus queue/DLQ depths (`raw-dmer-queue`, `extracted-dmer-queue`, topic subscriptions)
- Application Insights failure rate per service
- PostgreSQL Flexible Server CPU/storage/connection count

## Common operational tasks

| Task | Procedure |
|---|---|
| Reprocess a dead-lettered message | See `troubleshooting.md#dead-lettered-messages` |
| Rotate a rule version | Publish new `rules/versions/<version>/rules.json`, update `rules/active/rules.json`, verify via golden-file tests, monitor `rule_execution` for the new version |
| Rotate Managed Identity credentials | Not applicable — Managed Identity has no rotatable secret |
| Scale Container Apps | Adjust KEDA scale rule parameters in `deployment/<env>/parameters.json`, redeploy |

See `incident-response.md` for P1/P2 handling.
