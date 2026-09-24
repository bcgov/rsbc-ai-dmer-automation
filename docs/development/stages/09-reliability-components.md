# Stage 5 — Reliability Components

Source: architecture doc §4.6, §8. Figure: `docs/architecture/Figure1_Revised_Architecture.png`
("Reliability — no DMER outcome is ever lost"). Data model: `../data-model.md`. Messages:
`../message-contracts.md`. See also [DLQ Drain](10-dlq-drain.md), covered separately.

## Purpose

These are **not processing stages** — they exist so that the guarantee stated in the architecture
document's opening section holds:

> Once this solution is in production, Intake will look only at the AI-processed queue. A DMER
> whose outcome is never written back to Mercury is invisible to both the AI queue and the manual
> queue. It is not delayed; it is lost.

Every mechanism below exists to make that failure impossible. Their database writes are listed here
because a developer building them needs to know exactly which columns they own.

## Four layered mechanisms

1. **Outbox pattern** (this doc) — a decision and the intent to publish it commit together (see
   [Post-Processing](08-post-processing.md)); this doc covers the publisher that drains it.
2. **Reconciliation Sweeper** (this doc) — finds documents/drivers stuck mid-pipeline.
3. **Categorized failure handling with a guarded fallback outcome** — when a message dead-letters,
   the [DLQ Drain](10-dlq-drain.md) first *classifies* the failure (permanent business / transient
   infrastructure / processing / unknown). Only a **permanent business failure**, and only under the
   explicit business rule for un-processable DMERs (question I-1), emits a fallback outcome
   (`IN`, `decided_by = FALLBACK`) rather than stopping. Transient, processing, and unknown failures
   are made visible via `MANUAL_REVIEW` + a `processing_error` row + an alert and are routed to
   **operational recovery (bounded redrive)** or **manual review** — they never auto-produce a
   business decision. See [DLQ Drain](10-dlq-drain.md#failure-categorization-the-first-thing-the-drain-does).
4. **Daily reconciliation report** (this doc) — the evidence that the guarantee holds, checked
   against Mercury directly.

Each layer covers a failure the previous one cannot.

## Outbox Publisher

| | |
|---|---|
| Type | Azure Function, timer trigger, ~every 1 minute |
| Reads | `mercury_outbox` rows where `status = PENDING` and `next_attempt_at` has passed, ordered oldest first, in small batches |
| Writes | `mercury_outbox`, `dmer_document`, `driver_evaluation` |
| Calls | Mercury POST/PUT, with the idempotency key |

### Processing steps

1. Select `PENDING` outbox rows past their `next_attempt_at`, oldest first, in small batches.
2. Call the relevant Mercury POST/PUT with `idempotency_key`.
3. Record the response.

### Database writes

| Table | Operation | Fields |
|---|---|---|
| `mercury_outbox` | `UPDATE` | On success: `status = SENT`, `sent_at`, `mercury_response`. On failure: `attempt_count` incremented, `last_error`, `next_attempt_at` set by exponential backoff. After the attempt ceiling: `status = FAILED` and an alert. |
| `dmer_document` | `UPDATE` | `pipeline_status = COMPLETED` once **every** outbox row for the document is `SENT`. |
| `driver_evaluation` | `UPDATE` | `status = POSTED` once **every** outbox row in the batch is `SENT`. |

### Failure handling

A Mercury outage is a downstream-outage class failure (see `../message-contracts.md`) — back off
per row via `next_attempt_at`, alert on a growing count of `PENDING` rows past SLA (see
[Azure Monitor](../services/azure-monitor.md)), not on each individual failed attempt.

## Reconciliation Sweeper

| | |
|---|---|
| Type | Azure Function, timer trigger, ~every 15 minutes |
| Purpose | Converts "we believe nothing is stuck" into something demonstrable |

### The four queries

| Query | Action |
|---|---|
| Documents in a non-terminal `pipeline_status` whose `updated_at` is older than the stage SLA | Re-publish to the appropriate queue, increment `attempt_count`, alert after N sweeps. |
| `driver_evaluation` rows in `WAITING` where every document for that driver has in fact reached `RULES_APPLIED` | Re-signal the driver orchestration. **This is the direct remedy for the lost-update race** described in [Driver Orchestration](06-driver-orchestration.md#why-serialization-is-required). |
| `mercury_outbox` rows `PENDING` beyond a threshold, or with a high `attempt_count` | Alert — a sustained backlog here means Mercury is unavailable or rejecting. |
| Documents present in Mercury's `GET by driver_licence` response with no `dmer_document` row at all | Ingest them — this catches a lost webhook delivery and a page the poller skipped. |

### Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_document` | `UPDATE` | `attempt_count` incremented when a document is re-published to a queue; `updated_at`. |
| `driver_evaluation` | `UPDATE` | `status` `WAITING` → `STALE` when no progress past SLA, and `STALE` back to `READY` when re-signalled. |
| `processing_error` | `INSERT` | `document_id`, `stage`, `error_class` / `failure_category` (a sweeper-detected stall is `TRANSIENT` by nature — it never sets `PERMANENT_BUSINESS` and so never triggers a fallback decision), `reason_code`, `message`, `occurred_at` — recorded when a stall is detected, so repeated stalls on the same document are visible as a pattern. |

## Daily reconciliation report

Query Mercury for documents in the DPS queues with an empty `dps_date` and compare against
`dmer_decision`. Anything present in Mercury with no decision, or with a decision that was never
posted, appears on the report. **Run this from the first day in production**, not after the first
incident — it is the evidence that the guarantee holds.

## Worked example — the failure this design prevents

A driver has three DMERs. The first two process normally and sit in `AWAITING_DRIVER_COMPLETION`.
The third fails in extraction because the fax is unreadable.

- **Without this design:** the third document stops, the first two wait forever, and none of the
  three is ever posted. Intake sees nothing, and because the DMERs are no longer in the manual
  queue either, the driver is silently dropped.
- **With it:** the third document exhausts its retries and is dead-lettered with an explicit reason.
  The DLQ Drain classifies it as **`PERMANENT_BUSINESS`** (an unreadable fax can never succeed on
  retry), drains it to `MANUAL_REVIEW`, and — because the explicit business rule (question I-1)
  covers un-processable DMERs — queues a fallback `IN` outcome (`decided_by = FALLBACK`,
  `fallback_reason_code = PERMANENT_UNREADABLE_DOCUMENT`) to the outbox (see
  [DLQ Drain](10-dlq-drain.md)). It therefore reaches `RULES_APPLIED`-equivalent completeness for
  the purposes of the join. The driver evaluation becomes `READY`, the decision gateway runs, all
  three outcomes are written, and the outbox delivers them. Intake sees three DMERs, one flagged as
  needing human reading.

### The contrasting case this design also prevents

Now suppose the third document instead dead-letters because its lock expired while Postgres was
briefly unavailable — a **`TRANSIENT`** failure. The old design would have posted an `IN` fallback
here too, telling Mercury the driver is `IN` on the strength of an infrastructure blip. The revised
design does **not**: the transient failure is recorded in `processing_error`, the document goes to
`MANUAL_REVIEW`, an alert fires, and the message becomes a bounded **redrive** candidate for
operational recovery. No `dmer_decision` is written. Once the DB recovers the message is redriven and
processes normally; if redrive is exhausted it becomes `UNKNOWN` for human triage. A transient fault
never becomes a fabricated clinical outcome.

## Interaction with other stages

The Outbox Publisher drains what [Post-Processing](08-post-processing.md) writes. The Reconciliation
Sweeper is the safety net for [Ingest](01-ingest.md) (redelivery), [Extraction](02-extraction.md)/
[Document Orchestration](03-document-orchestration.md) (stalled documents), and
[Driver Orchestration](06-driver-orchestration.md) (stuck `WAITING` rows). It also feeds
[DLQ Drain](10-dlq-drain.md) (`processing_error` rows overlap in purpose but are populated by
different triggers — a sweeper-detected stall vs. a drained dead-letter message).

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `OUTBOX_POLL_INTERVAL_SECONDS` | Default ~60. |
| `OUTBOX_BATCH_SIZE` | Small batches — bounded by Mercury's unconfirmed concurrency limit (question M-10). |
| `OUTBOX_MAX_ATTEMPTS` / backoff schedule | |
| `SWEEPER_INTERVAL_MINUTES` | Default ~15. |
| `STAGE_SLA_*` | Per-stage SLA thresholds that define "stalled" — set once question I-14 (target time to AI-processed queue) is answered precisely. |

## Authentication / identity

Managed identity for PostgreSQL and Service Bus (republish). Mercury credentials via Key Vault for
both the Outbox Publisher's POST/PUT calls and the Sweeper's/report's read calls.

## Idempotency requirements

The Outbox Publisher's Mercury call must be safe to repeat — `idempotency_key` is the mechanism,
pending confirmation that Mercury actually honours it (question M-6). The Sweeper's re-publish path
reuses the same idempotent-upsert/replay-guard machinery as the original stage (see
[Ingest](01-ingest.md#idempotency-requirements), [Driver Orchestration](06-driver-orchestration.md#idempotency-requirements)) —
it does not need its own separate idempotency scheme.

## Logging / auditing

Every sweeper pass should log its four query result counts even when zero — a flat "0 stuck
documents" line every 15 minutes is what lets a dashboard show the sweeper is alive, distinct from
"no incidents." See [Azure Monitor](../services/azure-monitor.md) for the corresponding alert
thresholds (queue depth, DLQ count, oldest `PENDING` outbox row age, `driver_evaluation` rows in
`WAITING` past SLA).

## Implementation considerations for Claude Code

- No placeholder folder exists for these functions. Recommendation: a single small Azure Functions
  app (e.g. `services/reliability/`) hosting the Outbox Publisher, Reconciliation Sweeper, and
  (optionally) the Daily Reconciliation Report as three timer-triggered functions — they share no
  per-document processing logic with the other stages and are natural to deploy/scale together.
  Flagged as an [open question](#open-questions--decisions-required) since the architecture
  document doesn't name a service boundary for them.
- Both functions are read-heavy, DB-driven, low-per-invocation-cost — Consumption or Flex
  Consumption plan is likely sufficient even though other Function Apps in this pipeline need
  Premium for VNet integration; confirm VNet integration is still required here (it is, for private
  PostgreSQL/Service Bus access) before assuming a cheaper plan is viable.

## Open Questions / Decisions Required

- **Service boundary**: new `services/reliability/` app vs. folding these three timer functions into
  an existing app (e.g. `services/intake-processor/`, which is already a timer/HTTP Functions app).
  Not specified in the architecture document.
- **I-14** — target time from document arrival to appearing in the AI-processed queue: Intake's
  answer ("seconds or minutes... assume the SLA based on" the ~1M backlog) is directional, not a
  number. A concrete SLA is needed to set `STAGE_SLA_*` thresholds precisely.
- **I-15** — backlog size (~1M, oldest-first, confirmed) sets backfill concurrency/ordering
  expectations for the Reconciliation Sweeper's re-ingest query (documents present in Mercury with
  no `dmer_document` row) — make sure that query's ordering matches oldest-first during backfill,
  not just steady-state.
