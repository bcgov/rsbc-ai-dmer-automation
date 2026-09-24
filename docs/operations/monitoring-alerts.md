
# Monitoring & Alerts

> **Partially superseded.** Infra-level alerts below (Function failure rate, Container App
> restarts, PostgreSQL CPU/storage) are still valid. Queue-specific alerts reference the original
> architecture's queue names. See
> [`docs/development/services/azure-monitor.md`](../development/services/azure-monitor.md) for the
> revised alert set, including the outbox-age and driver-join-stuck alerts that have no equivalent
> below.

| Alert | Condition | Severity |
|---|---|---|
| DLQ depth | Any `$DeadLetterQueue` (queue or subscription) > 0 for 5 min | Sev 2 |
| DLQ drain — transient/processing/unknown failures | Any `processing_error` row with `failure_category IN ('TRANSIENT','PROCESSING','UNKNOWN')` in the last 15 min | Sev 2 |
| DLQ drain — unknown category | Any `processing_error` row with `failure_category = 'UNKNOWN'` (each one needs human triage; the drain could not classify it) | Sev 2 |
| Fallback decisions produced | Rate/count of `dmer_decision` rows with `decided_by = FALLBACK` above the agreed baseline over 1 h (a spike means many `PERMANENT_BUSINESS` failures — a model/input-quality regression) | Sev 3 |
| Redrive exhaustion | Any `processing_error` where `redrive_count` reached `DLQ_REDRIVE_MAX_ATTEMPTS` (a message could not be recovered and is now `UNKNOWN`) | Sev 2 |
| Function failure rate | > 5% over 15 min, any Function App | Sev 2 |
| Container App restarts | > 3 restarts in 10 min, any Container App | Sev 2 |
| PostgreSQL CPU | > 80% for 15 min | Sev 3 |
| PostgreSQL storage | > 85% allocated | Sev 3 |
| Document Intelligence throttling | `429` rate > 1% over 15 min | Sev 3 |
| External Azure OpenAI throttling/errors | `429`/5xx rate from the external AI Hub endpoint > 1% over 15 min | Sev 3 |
| End-to-end processing latency | p95 > SLA target | Sev 3 |

The four DLQ-drain alerts above encode the safety rule operationally: a **transient, processing, or
unknown** dead-letter must page someone because it is *not* self-resolved by a fallback decision,
and a spike in **fallback decisions** must be visible because each one is a DMER the AI could not
process. See
[DLQ Drain §Failure categorization](../development/stages/10-dlq-drain.md#failure-categorization-the-first-thing-the-drain-does).

All alerts route to the ops Action Group defined in
`infrastructure/bicep/modules/monitor/alerts.bicep` (email + Teams webhook, environment-specific).

Dashboards/workbooks live in `monitoring/dashboards/` and `monitoring/workbooks/`.
