
# Operations Runbook

## Daily checks

- The daily reconciliation report (`reconciliation-reports/<date>.json` on the runtime storage
  account): `findings` should be empty -- see [below](#daily-reconciliation-report)
- Service Bus queue/DLQ depths (`dmer-ingest`, `dmer-raw`, `dmer-extracted`, `driver-decision`)
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

## Alerts

The rules are listed in [monitoring-alerts.md](monitoring-alerts.md). Queries below run against
the `dmer` database; ids and statuses are safe to share, outbox payloads and comments are not
(clinical content).

### `outbox-write-failed`

Mercury rejected an outcome write (a 4xx), or it ran out of attempts. The row is `FAILED`, any
rows behind it for the same document are `FAILED` as `blocked: ...`, and the document stays in
`POSTING`. Nothing in the system retries it: **a person resolves it in Mercury**.

1. Find the rows: `SELECT document_id, operation, attempt_count, last_error FROM mercury_outbox
   WHERE status = 'FAILED';`
2. Read what was meant to be written (`payload`: the outcome, the comment, the case or driver),
   and the reason in `last_error` (the HTTP status).
3. Apply it in Mercury by hand -- the outcome, the duplicate status, the driver mapping or the
   case, as the failed `operation` says.
4. Record the resolution outside the system (the ticket). The row stays `FAILED`; the alert fires
   once per row, not again.

If many rows fail at once, Mercury's API changed or is rejecting everything: stop the publisher
(`AzureWebJobs.OutboxPublisher.Disabled = true` on the reliability app) before working through
them.

### `outbox-backlog`

Writes are waiting past the SLA or being retried many times: Mercury is unavailable, slow or
throttling. Check the publisher's `outbox row will be retried` lines (`http_status`) and Mercury's
status. The rows retry by themselves with backoff; nothing is lost while they wait.

### `dlq-needs-triage` / `stalled-document-manual-review`

A document went to `MANUAL_REVIEW` without an outcome being sent to Mercury. Find why:
`SELECT stage, failure_category, reason_code, message, resolution FROM processing_error WHERE
document_id = '<id>' ORDER BY id;` Then either process the DMER by hand in Mercury, or fix the
cause and re-send the message (`scripts/debug/sb_dlq_resubmit.py`).

### `dlq-retrying` / `driver-evaluation-stuck`

The system is recovering on its own (bounded retries, re-signals). Watch for a cluster: many in a
short time means a dependency (DI, the OpenAI gateway, Mercury, PostgreSQL) is failing.

### Daily reconciliation report

`DailyReconciliation` (reliability app) checks every document in Mercury's DPS queue against the
pipeline and writes `reconciliation-reports/<yyyy-mm-dd>.json`. Each entry in `entries` has a
`finding`:

| Finding | Meaning | Action |
|---|---|---|
| `NOT_RECEIVED` | In Mercury for over 2 h, never ingested | Check the Page Poller; it is re-ingested on the next poll of its page |
| `OVERDUE` | Still in the pipeline 24 h after we first saw it | Check `processing_error` and the sweeper's logs for the document |
| `MANUAL_REVIEW_NO_OUTCOME` | In manual review with nothing sent to Mercury -- Intake can't see it | Process it by hand in Mercury (see `dlq-needs-triage`) |
| `NOT_POSTED` | An outcome is queued but not all writes are sent after 24 h | See `outbox-backlog` |
| `POST_FAILED` | A Mercury write is `FAILED` | See `outbox-write-failed`; the document stays on the report until Mercury has triaged it |

`mercury_read_complete: false` means the queue was longer than `RECONCILIATION_MAX_PAGES` pages
and only part of it was checked. `metrics` holds the day's manual reviews, fallback outcomes and
stage p95s (the `manual-review-rate`, `fallback-rate` and `stage-p95` alerts).
