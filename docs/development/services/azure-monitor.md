# Azure Monitor / Application Insights

Source: architecture doc §3.2, §9.2, §9.3. Bicep modules:
`infrastructure/bicep/modules/monitor/{log-analytics-workspace,application-insights,diagnostic-settings,alerts}.bicep`.

## Role

Logs, metrics, and alerting for every component in the pipeline. One shared Log Analytics
Workspace per environment; every resource's diagnostic settings point to it, so a single query can
trace one document (via `correlation_id`) through every stage it passed through.

## Structured logging

`libs/dmer_common/src/dmer_common/telemetry/logging.py` (`get_logger`) is the shared logger every
stage should use — already implements:

- JSON-formatted log lines (`JsonFormatter`) with `timestamp`, `level`, `logger`, `message`,
  `correlation_id`, plus any extra fields.
- Correlation-ID propagation via a context variable (`correlation_context`), so it doesn't need to
  be threaded through every function call manually — bind it once per message/document at the top
  of a handler.
- **PII redaction** (`PiiRedactionFilter`, `DEFAULT_PII_FIELDS`) — any log field or nested key named
  `name`, `dob`, `phn`, `address`, `diagnosis`, `handwritten`, `ocr_text`, `content`, etc. is
  replaced with `[REDACTED]` automatically. This is the enforcement mechanism for §9.2's "no
  clinical content or licence numbers in Application Insights traces" rule — **do not bypass it by
  logging raw dicts through a different logger**.

This module is architecture-agnostic and needs no rework for the revised architecture — every new
stage should call `get_logger(__name__)` and rely on its redaction rather than hand-rolling logging.

## Metrics & alerts

| Signal | Why it matters | Alert when |
|---|---|---|
| Active message count per queue | Earliest indicator of a stalled stage | Sustained growth over a defined window |
| Dead-letter message count | Poison input or a systemic failure | Any non-zero value, and separately on rate of change |
| DLQ-drain failures by `failure_category` | Distinguishes a permanent-business failure (expected, produces a guarded fallback) from a transient/processing/unknown failure (must not, and needs recovery or human triage) | Any `TRANSIENT`/`PROCESSING`/`UNKNOWN` row; each `UNKNOWN` individually |
| Fallback decisions (`decided_by = FALLBACK`) | Every one is a DMER the AI could not process; a spike is a model/input-quality regression | Rate above the agreed baseline — see [DLQ Drain](../stages/10-dlq-drain.md) |
| Oldest `PENDING` outbox row age | Direct measure of the no-lost-outcome guarantee | Older than the agreed SLA to Intake |
| `driver_evaluation` rows in `WAITING` past SLA | The join is stuck | Any row beyond threshold after a sweeper pass |
| Stage duration p50/p95 from `dmer_stage_run` | Capacity planning and quota pressure | p95 beyond the modelled budget |
| Document Intelligence and Azure OpenAI 429 rate | Quota is the throughput ceiling | Sustained throttling |
| Documents in `MANUAL_REVIEW` per day | Model or input quality regression | Above the agreed baseline (question I-16: extraction confidence surfaced as a decision-reason comment, not yet a dashboard threshold) |

These **replace** the older alert table in `docs/operations/monitoring-alerts.md` (Function failure
rate, Container App restarts, PostgreSQL CPU/storage remain valid infra-level alerts and should be
kept **alongside** the table above, not instead of it — see
[Alignment gaps](#alignment-gaps-vs-current-code)).

## Daily reconciliation report

Not a dashboard metric — a scheduled query/report comparing Mercury's DPS-queue documents (empty
`dps_date`) against `dmer_decision`. See
[Reliability Components §Daily reconciliation report](../stages/09-reliability-components.md#daily-reconciliation-report).
Run this from day one in production.

## Workbooks

`monitoring/workbooks/` (placeholder) should include rule-decision distribution and normalization
confidence trend — business-outcome signals, not just infrastructure health
(`docs/architecture/repository-design.md` §14).

## Authentication / identity

Diagnostic settings write via resource-level RBAC, not application code — no credentials needed at
the application layer beyond the Application Insights connection string (itself a non-secret
configuration value, safe in App Configuration).

## Security considerations

No clinical content or licence numbers in traces — enforced by `PiiRedactionFilter`, not by
discipline alone. If a new log call needs a field not in `DEFAULT_PII_FIELDS` but that turns out to
carry sensitive content, add it to that frozenset rather than special-casing the call site.

## Implementation considerations for Claude Code

- Every stage/activity should call `dmer_common.telemetry.get_logger(__name__)` and bind
  `correlation_id` via `correlation_context()` at the start of message/activity handling — see each
  stage doc's "Logging / auditing" section for what's worth logging at INFO vs. keeping DB-only.
- `infrastructure/bicep/modules/monitor/alerts.bicep` exists but has no alert rules instantiated
  yet matching the table above — build these from the four-queue names in `../message-contracts.md`
  and the specific queries in
  [Reliability Components §The four queries](../stages/09-reliability-components.md#the-four-queries).

## Alignment gaps vs. current code

`docs/operations/monitoring-alerts.md` (old) lists alerts scoped to the original architecture's two
queues + one topic (`raw-dmer-queue`, `extracted-dmer-queue`, `dmer-lifecycle-events`) and its
service names (`di-processor`, `normalizer-service`, etc.). Update queue-name references to the four
revised queue names (`dmer-ingest`, `dmer-raw`, `dmer-extracted`, `driver-decision`) and add the
outbox-age and `driver_evaluation`-stuck alerts above, which have no equivalent in the old table
(the original architecture had no per-driver join and no outbox pattern).
