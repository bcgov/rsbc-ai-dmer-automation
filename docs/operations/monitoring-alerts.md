
# Monitoring & Alerts

| Alert | Condition | Severity |
|---|---|---|
| DLQ depth | Any `$DeadLetterQueue` (queue or subscription) > 0 for 5 min | Sev 2 |
| Function failure rate | > 5% over 15 min, any Function App | Sev 2 |
| Container App restarts | > 3 restarts in 10 min, any Container App | Sev 2 |
| PostgreSQL CPU | > 80% for 15 min | Sev 3 |
| PostgreSQL storage | > 85% allocated | Sev 3 |
| Document Intelligence throttling | `429` rate > 1% over 15 min | Sev 3 |
| External Azure OpenAI throttling/errors | `429`/5xx rate from the external AI Hub endpoint > 1% over 15 min | Sev 3 |
| End-to-end processing latency | p95 > SLA target | Sev 3 |

All alerts route to the ops Action Group defined in
`infrastructure/bicep/modules/monitor/alerts.bicep` (email + Teams webhook, environment-specific).

Dashboards/workbooks live in `monitoring/dashboards/` and `monitoring/workbooks/`.
