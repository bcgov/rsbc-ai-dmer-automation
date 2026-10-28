# Monitoring & Alerts

The alert rules are `infrastructure/bicep/modules/monitor/alerts.bicep`, deployed by
`infrastructure/bicep/monitoring.bicep` with `deployment/<env>/monitoring.parameters.json`. They
implement the catalogue in [`docs/development/services/azure-monitor.md`](../development/services/azure-monitor.md)
("Metrics & alerts"). Every rule notifies the `ag-rsbc-dmer-<env>-pipeline` Action Group (the
`alertEmailAddresses` parameter). What to do when one fires: [runbook.md](runbook.md#alerts).

The pipeline apps log one JSON object per line; it arrives in Log Analytics as
`AppTraces.Message`, so the log alerts match the line's `message` and read its fields.

## Metric alerts

| Rule (`rsbc-dmer-<env>-...`) | Condition | Severity |
|---|---|---|
| `dead-letter` | Any pipeline queue's dead-letter count > 0 (max over 15 min) | Sev 2 |
| `queue-backlog` | A queue's active messages > `queueBacklogThreshold` (500), averaged over 30 min | Sev 3 |
| `postgres-cpu` | PostgreSQL CPU > 80% over 15 min | Sev 2 |
| `postgres-storage` | PostgreSQL storage > 80% | Sev 2 |
| `document-intelligence-throttled` | Document Intelligence `BlockedCalls` (refused for rate/quota) > 0 in 15 min | Sev 3 |

## Log alerts

| Rule (`rsbc-dmer-<env>-...`) | Fires on | Severity |
|---|---|---|
| `outbox-write-failed` | The Outbox Publisher gave up on a Mercury write (`FAILED`) -- once per row | Sev 1 |
| `outbox-backlog` | The sweeper found outcome writes pending past the SLA or retried many times | Sev 1 |
| `reconciliation-unaccounted` | The daily report found documents in Mercury's DPS queue not accounted for, or couldn't read the whole queue | Sev 1 |
| `dlq-needs-triage` | The DLQ Drain sent a document to manual review (`UNKNOWN`: logical error, unknown reason, retries used up) | Sev 2 |
| `stalled-document-manual-review` | The sweeper gave up re-publishing a stalled document | Sev 2 |
| `reconciliation-not-run` | No daily reconciliation report in the last day | Sev 2 |
| `function-failures` | More than `functionFailureThreshold` (5) failed invocations on one app in 15 min | Sev 2 |
| `dlq-retrying` | The DLQ Drain is retrying dead letters (`TRANSIENT`/`PROCESSING`) | Sev 3 |
| `driver-evaluation-stuck` | A driver evaluation was stuck in `WAITING` past the SLA after a sweeper pass | Sev 3 |
| `manual-review-rate` | Daily `MANUAL_REVIEW` count > `manualReviewDailyThreshold` (50; question I-16) | Sev 3 |
| `fallback-rate` | Daily fallback outcomes > `fallbackDailyThreshold` (20) | Sev 3 |
| `stage-p95` | A stage's p95 duration over the day > `stageP95SecondsThreshold` (900 s) | Sev 3 |

The DLQ-drain alerts encode the safety rule operationally: a transient, processing or unknown
dead letter is never resolved by a fallback decision, so it must reach a person; a spike in
fallback outcomes is visible because each is a DMER the AI could not process. See
[DLQ Drain §Failure categorization](../development/stages/10-dlq-drain.md#failure-categorization-the-first-thing-the-drain-does).

## Not covered

- **The external Azure OpenAI gateway (AI Hub)** is not our resource to put a metric alert on, and
  normalization doesn't log its 429s; its throttling shows as manual reviews and stage p95.
- **Container App restarts**: the pipeline runs on Function Apps; there are no Container Apps.

Dashboards/workbooks live in `monitoring/dashboards/` and `monitoring/workbooks/`.
