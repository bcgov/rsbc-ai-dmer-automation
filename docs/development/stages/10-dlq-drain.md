# DLQ Drain

Source: architecture doc §4.6.3, §6. Data model: `../data-model.md`. Messages:
`../message-contracts.md`. See also [Reliability Components](09-reliability-components.md).

## Purpose

Reads every dead-letter sub-queue and converts each message into a **recorded, visible outcome**
rather than a silent loss. This is the mechanism that makes the fallback-outcome guarantee
(architecture document §8.3) concrete: "failure to decide must not mean failure to appear."

**"Visible" does not always mean "decided."** A dead-lettered message is a signal that *something*
failed — but the failure can be a permanent property of the input (an unreadable PDF), or a
transient property of the infrastructure (a lock expired, Postgres was briefly unavailable, a
downstream dependency was throttling). These demand different remedies. Only a **permanent business
failure with an explicit business rule** may be turned into a fallback business decision. Every
other category is made visible by routing it to **manual review or operational recovery** — never
by inventing an `IN` outcome and posting it to Mercury as if the pipeline had decided it.

> **Safety rule (do not weaken without a business sign-off):** a fallback business decision is only
> ever produced for the `PERMANENT_BUSINESS` failure category, and only because an explicit business
> rule (question I-1) says an un-processable DMER defaults to `IN` with the comment "AI could not
> process". Transient, processing, and unknown failures must **never** auto-produce an `IN` (or any
> other) business outcome — writing one would let an infrastructure blip masquerade as a considered
> clinical intake decision. This decision is recorded in
> [ADR-0002](../../architecture/decision-records/0002-dlq-fallback-decisions-gated-by-failure-category.md).

## Why dead-letter queues need an explicit drain

Every Service Bus queue and topic subscription has a dead-letter sub-queue at
`<entity>/$DeadLetterQueue`, provisioned automatically. Three things teams get wrong:

- **Dead-letter queues do not drain themselves.** Messages stay indefinitely and count against the
  entity size quota. An undrained DLQ eventually fills and blocks the main queue.
- **There is no native requeue.** Resubmitting means receiving from the dead-letter path and
  sending a new message to the main entity. Service Bus Explorer has a button that does this, but
  relying on someone clicking in the portal is not an operational process.
- **Without explicit dead-lettering there is no reason recorded.** A message that reached
  `MaxDeliveryCount` carries a system reason, not an application one — always dead-letter
  known-bad input explicitly with a description (see `../message-contracts.md#dead-letter-handling`).

## Trigger / reads / writes

| | |
|---|---|
| Type | Azure Function, reads every queue's `$DeadLetterQueue` (all four: `dmer-ingest`, `dmer-raw`, `dmer-extracted`, `driver-decision`) |
| Writes | `processing_error`, `dmer_document`, `dmer_decision` (**only** for the `PERMANENT_BUSINESS` category), `mercury_outbox` (**only** for the `PERMANENT_BUSINESS` category) |

## Failure categorization (the first thing the drain does)

Before deciding what to do with a dead-lettered message, classify it. The dead-letter reason and
description (set via `DeadLetterMessageAsync(reason, description)` — see
`../message-contracts.md#dead-letter-handling`), the `MaxDeliveryCount`-exceeded system reason, and
the last `dmer_stage_run.error_code` for the document together determine the category:

| Category | `error_class` | What it means | Examples |
|---|---|---|---|
| **Permanent business error** | `PERMANENT_BUSINESS` | The input can never succeed no matter how many times it is retried, and a business rule defines the outcome. | Unreadable/malformed PDF, `document_guid` that no longer exists in Mercury, schema violation that is a property of the content. |
| **Transient infrastructure error** | `TRANSIENT` | A temporary environmental condition; the same message would likely succeed on a clean retry. | Lock/session expiration mid-processing, Postgres connection/deadlock failure, Service Bus lock lost, DNS/network blip, `MaxDeliveryCount` reached purely because of repeated timeouts. |
| **Processing error** | `PROCESSING` | Retries were exhausted against a *dependency* that is (or was) down or throttling — not permanently bad input, but not resolved. | DI/OpenAI/Mercury `429` or 5xx past the internal retry budget, downstream outage that outlasted the queue's TTL. |
| **Unknown error** | `UNKNOWN` | The reason cannot be mapped to any of the above with confidence (missing/garbled dead-letter reason, unexpected exception class). | Handler crashed without an explicit dead-letter reason and no matching `dmer_stage_run` error; `MaxDeliveryCount` system reason with no correlating application error. |

Classification is conservative: **if a message cannot be confidently classified as
`PERMANENT_BUSINESS`, it is not.** `UNKNOWN` is the default, not `PERMANENT_BUSINESS`. See
[Classification precedence](#classification-precedence) below.

## Processing steps, per dead-lettered message

1. **Classify** the failure into one of the four categories above (see the table).
2. Write a `processing_error` row with `document_id`, `stage`, `error_class`, `failure_category`,
   `reason_code`, the dead-letter reason, and the original message id.
3. Route by category:
   - **`PERMANENT_BUSINESS`** — set `pipeline_status = MANUAL_REVIEW`, and (only here) queue a
     **fallback outcome** to `mercury_outbox`: `IN`, `decided_by = FALLBACK`, with a `reason_code`
     and the Mercury comment **"AI could not process"** (question I-1, answered), written back as
     part of the POST/PUT — not just logged. This is the one category where making failure visible
     means producing a decision.
   - **`TRANSIENT`** — do **not** write any decision. Set `pipeline_status = MANUAL_REVIEW` (so the
     document is visible and can be redriven) and hand the message to **operational recovery**: it
     is a candidate for safe redrive to the main queue once the transient condition clears (a fresh
     lock, a healthy DB). Redrive is bounded and audited — see
     [Operational recovery vs. fallback](#operational-recovery-vs-fallback-decision).
   - **`PROCESSING`** — do **not** write any decision. Set `pipeline_status = MANUAL_REVIEW`, record
     the dependency and the exhausted-retry context, and alert. A sustained cluster here means a
     dependency is down; the remedy is fixing/redriving, not deciding.
   - **`UNKNOWN`** — do **not** write any decision. Set `pipeline_status = MANUAL_REVIEW`, record the
     raw reason verbatim, and alert for human triage. An operator decides whether it is really a
     permanent business failure (then it can be manually resolved to a fallback with `decided_by =
     MANUAL`) or a redrive candidate.
4. Raise an alert if the dead-letter rate crosses a threshold, and **separately** if any non-permanent
   category (`TRANSIENT`/`PROCESSING`/`UNKNOWN`) appears at all — a spike there almost always means a
   systemic problem, and none of those should ever have silently become a business decision.

### Classification precedence

1. An **explicit** application dead-letter reason (`DeadLetterMessageAsync(reason, description)`)
   naming a poison/business condition → `PERMANENT_BUSINESS`.
2. An explicit reason naming a dependency 429/5xx/outage → `PROCESSING`.
3. A `MaxDeliveryCount`-exceeded **system** reason correlated with `dmer_stage_run` timeouts /
   lock-lost / DB errors → `TRANSIENT`.
4. Anything else, including a `MaxDeliveryCount` system reason with no correlating application error
   → `UNKNOWN`.

The rule of thumb: **only path 1 may produce a business decision.** Everything else is visible-but-
undecided until a human or an automated redrive resolves it.

### Operational recovery vs. fallback decision

"Operational recovery" means the message is eligible to be re-sent to its origin main queue (a real
requeue: receive from `$DeadLetterQueue`, publish a fresh message with `attempt` incremented — there
is no native requeue) once the transient/processing condition is believed cleared. It is bounded by
`DLQ_REDRIVE_MAX_ATTEMPTS`; a message that exhausts redrive attempts is re-categorized `UNKNOWN` and
left for human triage. Redrive never writes a `dmer_decision`. This is the safe counterpart to the
fallback path: the fallback *decides*, recovery *retries*.

## Database writes

| Table | Operation | Category | Fields |
|---|---|---|---|
| `processing_error` | `INSERT` | **all** | `document_id`, `stage`, `error_class` / `failure_category` (`PERMANENT_BUSINESS`, `TRANSIENT`, `PROCESSING`, `UNKNOWN`), `reason_code`, `message` (raw dead-letter reason), `dlq_message_id`, `occurred_at`. |
| `dmer_document` | `UPDATE` | **all** | `pipeline_status = MANUAL_REVIEW`, `updated_at`. |
| `dmer_decision` | `INSERT` | **`PERMANENT_BUSINESS` only** | `outcome_code = IN`, `decided_by = FALLBACK`, `fallback_reason_code`, `decision_reason` (jsonb: category, failed stage, dead-letter reason). Governed by question I-1's confirmed answer above. **Never written for `TRANSIENT`, `PROCESSING`, or `UNKNOWN`.** |
| `mercury_outbox` | `INSERT` | **`PERMANENT_BUSINESS` only** | `operation = UPDATE_OUTCOME` carrying the fallback outcome and the "AI could not process" comment, so the document still reaches Intake. **Never written for the other three categories.** |

Note this inserts `dmer_decision` and `mercury_outbox` directly — unlike the normal path where only
[Post-Processing](08-post-processing.md) writes these tables. DLQ Drain is the one other writer, and
**only for the `PERMANENT_BUSINESS` category**. For the same reason Post-Processing uses a single
transaction: when a fallback *is* produced, **the fallback decision and the intent to deliver it
must commit together** (and together with the `processing_error` row and the `MANUAL_REVIEW`
update), or the fallback path reintroduces the exact invisible-DMER failure it exists to close. For
the other three categories the transaction writes only `processing_error` + the `MANUAL_REVIEW`
update — no decision, no outbox row.

### Why the other categories do not get a decision

A `TRANSIENT` or `UNKNOWN` failure that is auto-converted to `IN` posts a **real, considered-looking
clinical intake outcome** to Mercury on the basis of an infrastructure blip. That is worse than an
undrained DLQ: the DMER now looks decided, so neither the AI queue nor the manual queue flags it for
reading, and the reconciliation report sees a decision and moves on. The no-lost-outcome guarantee is
satisfied by the `MANUAL_REVIEW` status + `processing_error` row + alert, which make the failure
*visible and actionable* without fabricating a decision. A business decision requires a business rule;
there is no business rule that says "a lock expired, therefore this driver is `IN`."

## Pre-signed URL replay caveat

If a message dead-letters at [Ingest](01-ingest.md) and is replayed hours later, it may fail again
on an **expired pre-signed URL** (question M-1, open — see `01-ingest.md#failure-handling`). If the
TTL is short and no fresh-URL mechanism exists, DLQ Drain's remedy for an Ingest-stage dead letter
is **not** "resubmit the original message" but "re-poll that document through the Mercury batch API
to obtain a new URL." Build this branch from the start; do not discover the need during the first
incident.

Crucially, an expired-URL failure is **`TRANSIENT`**, not `PERMANENT_BUSINESS` — the document itself
is fine, only the URL went stale. It must be routed to operational recovery (re-poll + redrive), and
it must **never** receive a fallback `IN` decision. Misclassifying it as permanent would fabricate a
business outcome for a document that could have processed cleanly with a fresh URL.

## Interaction with upstream/downstream

Reads from every queue's DLQ across all stages. Its `mercury_outbox` writes are drained by the
[Outbox Publisher](09-reliability-components.md#outbox-publisher) exactly like a normal decision.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `SERVICEBUS_NAMESPACE` | Same namespace as the four main queues. |
| `DLQ_DRAIN_INTERVAL` | Timer or continuously-running peek-lock consumer per DLQ — not specified in the architecture document; timer-based (e.g. every 5 minutes) is simplest and matches the other reliability functions' pattern. |
| `DLQ_ALERT_THRESHOLD` | Dead-letter rate that triggers an alert — see [Azure Monitor](../services/azure-monitor.md). |
| `FALLBACK_OUTCOME_CODE` | Default `IN`. **Only applied to the `PERMANENT_BUSINESS` category.** |
| `FALLBACK_ENABLED_CATEGORIES` | The failure categories permitted to produce a fallback business decision. **Default and only supported value: `PERMANENT_BUSINESS`.** A deliberate config gate so no other category can be silently enabled without a documented business rule change. |
| `DLQ_REDRIVE_MAX_ATTEMPTS` | Bounded redrive count for `TRANSIENT`/`PROCESSING` operational recovery before a message is re-categorized `UNKNOWN` for human triage. |

## Authentication / identity

Managed identity for Service Bus (dead-letter sub-queue receive across all four queues) and
PostgreSQL.

## Idempotency requirements

Draining the same dead-lettered message twice must not create two `processing_error` rows or two
fallback decisions for the same document. Concretely:

- **`processing_error`** is keyed on `(document_id, stage, dlq_message_id)` (unique) so a re-drain of
  the same dead-lettered message is a no-op insert, not a duplicate row.
- **The fallback `dmer_decision`** (only ever written for `PERMANENT_BUSINESS`) is guarded so a
  document that already has *any* decision — a prior fallback, or a real AI/`MANUAL` decision that
  landed in the meantime — is **not** given a second one. Key the fallback write on `document_id`
  with a conflict-check (`INSERT ... WHERE NOT EXISTS (SELECT 1 FROM dmer_decision WHERE
  document_id = :doc)`), so a redrive after a human already resolved the document cannot overwrite
  or duplicate the human's decision (see the immutability rule in
  [Post-Processing](08-post-processing.md#decision-immutability-after-human-review-question-i-2-answered)).
- **The fallback `mercury_outbox`** row carries a deterministic `idempotency_key` derived from
  `document_id` + `FALLBACK` so a re-drain that slips past the decision guard still cannot cause a
  second Mercury post (the outbox unique key rejects it).
- **Redrive** (operational recovery) is bounded by `DLQ_REDRIVE_MAX_ATTEMPTS` and increments
  `attempt` on the republished envelope, so a message cannot be redriven forever; the count is the
  duplicate-decision-prevention mechanism for the recovery path.

The upshot: **a document gets at most one decision, ever, regardless of how many times its messages
dead-letter and drain** — and a transient/unknown drain contributes *zero* decisions.

## Logging / auditing

Every drain is, by definition, a failure that reached the end of the line — log at WARN or ERROR
with `document_id`, `stage`, `failure_category`, `reason_code`, `dlq_message_id`, and the dead-letter
reason string. When a fallback decision is written, log an explicit **audit line** at WARN naming
`decided_by = FALLBACK`, the `fallback_reason_code`, the failed stage, and the `idempotency_key` —
this is the record an incident review greps for to answer "why does this driver have an `IN` nobody
chose." Drains that do **not** produce a decision (`TRANSIENT`/`PROCESSING`/`UNKNOWN`) must log the
category and the routing outcome (`MANUAL_REVIEW` / redrive) so it is unambiguous that no decision
was fabricated.

This is the primary input to "documents in `MANUAL_REVIEW` per day" and to the new
per-category drain metrics (see [Azure Monitor](../services/azure-monitor.md)), which are a
model/input-quality regression signal (question I-16) as well as an infrastructure-health signal.

## Implementation considerations for Claude Code

- No placeholder folder exists for this function. Group it with the other reliability functions —
  see `09-reliability-components.md#implementation-considerations-for-claude-code`'s proposed
  `services/reliability/` app; DLQ Drain fits the same low-per-invocation, DB-and-queue-driven
  profile as the Outbox Publisher and Reconciliation Sweeper.
- Reading a Service Bus dead-letter sub-queue is a normal `ServiceBusReceiver` pointed at
  `<queue>/$DeadLetterQueue` — `libs/dmer_common/src/dmer_common/messaging/consumer.py`'s settlement
  logic (complete/dead-letter) doesn't directly apply here (there's no "re-dead-letter" path); this
  function completes each DLQ message after recording it, rather than routing it further.

## Example payload — fallback `mercury_outbox.payload` (`PERMANENT_BUSINESS` only)

Produced **only** when the failure is classified `PERMANENT_BUSINESS`. No such payload is ever
generated for `TRANSIENT`, `PROCESSING`, or `UNKNOWN`.

```json
{
  "document_guid": "123e4567-e89b-...",
  "outcome_code": "IN",
  "comment": "AI could not process",
  "decided_by": "FALLBACK",
  "fallback_reason_code": "PERMANENT_UNREADABLE_DOCUMENT",
  "failure_category": "PERMANENT_BUSINESS",
  "failed_stage": "EXTRACT",
  "duplicate_of": null
}
```

### Reason codes

`fallback_reason_code` (and the `reason_code` on `processing_error`) is a stable, enumerated string
so decisions and failures are queryable and auditable rather than free-text-only:

| `reason_code` | Category | Meaning |
|---|---|---|
| `PERMANENT_UNREADABLE_DOCUMENT` | `PERMANENT_BUSINESS` | PDF is malformed/unreadable and cannot be extracted. |
| `PERMANENT_SCHEMA_VIOLATION` | `PERMANENT_BUSINESS` | Content violates the extraction schema in a non-recoverable way. |
| `PERMANENT_DOCUMENT_GONE` | `PERMANENT_BUSINESS` | `document_guid` no longer exists in Mercury. |
| `TRANSIENT_LOCK_EXPIRED` | `TRANSIENT` | Lock/session lost mid-processing. |
| `TRANSIENT_DB_FAILURE` | `TRANSIENT` | Postgres connection/deadlock/timeout. |
| `TRANSIENT_DELIVERY_EXHAUSTED` | `TRANSIENT` | `MaxDeliveryCount` reached via repeated timeouts, no permanent cause found. |
| `PROCESSING_DEPENDENCY_THROTTLED` | `PROCESSING` | DI/OpenAI/Mercury `429` past the retry budget. |
| `PROCESSING_DEPENDENCY_UNAVAILABLE` | `PROCESSING` | Downstream 5xx/outage outlasted TTL. |
| `UNKNOWN_UNCLASSIFIED` | `UNKNOWN` | Reason could not be mapped; needs human triage. |

Only `PERMANENT_BUSINESS` reason codes may accompany a `dmer_decision`. The rest accompany a
`processing_error` row and a routing action, never a decision.

## Required behaviour-spec tests

Every acceptance criterion below must have a corresponding test (GIVEN/WHEN/THEN), per the
[coding standards](../coding-standards.md#testing) and global steering §12. These are the tests the
generated stubs under `services/reliability/tests/` must cover before this function ships:

| GIVEN | WHEN | THEN |
|---|---|---|
| a dead-lettered message with an explicit poison reason (unreadable PDF) | the drain classifies and routes it | it is categorized `PERMANENT_BUSINESS`, a fallback `dmer_decision` (`IN`, `decided_by = FALLBACK`, `fallback_reason_code` set) **and** a `mercury_outbox` row are written in one transaction, and `pipeline_status = MANUAL_REVIEW` |
| a dead-lettered message whose lock expired mid-processing | the drain classifies and routes it | it is categorized `TRANSIENT`, **no** `dmer_decision` and **no** `mercury_outbox` row are written, a `processing_error` row is written, `pipeline_status = MANUAL_REVIEW`, and it is queued for bounded redrive |
| a dead-lettered message from a dependency 429/5xx past the retry budget | the drain classifies and routes it | it is categorized `PROCESSING`, **no** decision/outbox row is written, and an alert-worthy `processing_error` row is written |
| a dead-lettered message with a `MaxDeliveryCount` system reason and no correlating application error | the drain classifies it | it is categorized `UNKNOWN` (the conservative default), **no** decision is written, and it is routed to human triage |
| a document that already has a `dmer_decision` (AI, MANUAL, or a prior FALLBACK) | a `PERMANENT_BUSINESS` message for it is drained again | **no** second decision is written (the `WHERE NOT EXISTS` guard / unique index holds) and the existing decision is unchanged |
| the same dead-lettered message | it is drained twice | exactly one `processing_error` row exists (unique on `(document_id, stage, dlq_message_id)`) and at most one Mercury post is attempted (outbox `idempotency_key`) |
| a `TRANSIENT` message that has already been redriven `DLQ_REDRIVE_MAX_ATTEMPTS` times | the drain processes it again | it is re-categorized `UNKNOWN` and routed to human triage rather than redriven again — still **no** decision |
| `FALLBACK_ENABLED_CATEGORIES` set to its only supported value | any non-`PERMANENT_BUSINESS` message is drained | no configuration path exists that lets it produce a business decision |

## Assumptions made in this revision

- **Assumption A-1:** the *only* explicit business rule permitting a fallback decision is question
  I-1 ("un-processable DMER defaults to `IN` with comment 'AI could not process'"). No business rule
  authorizes a fallback for transient/processing/unknown failures, so this revision introduces none.
  If Intake later defines such a rule, add the category to `FALLBACK_ENABLED_CATEGORIES` and document
  the rule here first — do not enable it in code alone.
- **Assumption A-2:** classification inputs available at drain time are the dead-letter
  reason/description, the `MaxDeliveryCount` system reason, and the document's last
  `dmer_stage_run.error_code`. If richer signals become available, they refine classification but do
  not relax the safety rule.
- **Assumption A-3:** an unreadable/malformed PDF and a `document_guid` that no longer exists in
  Mercury are genuinely permanent (retry can never succeed). If Ingest gains a fresh-URL-on-demand
  mechanism (question M-1), a document that dead-lettered on an *expired pre-signed URL* is
  `TRANSIENT`, not `PERMANENT_BUSINESS`, and must be redriven after re-polling Mercury — not given a
  fallback decision.

## Open Questions / Decisions Required

- **M-1** — pre-signed URL TTL / fresh-URL-on-demand availability (see
  [Pre-signed URL replay caveat](#pre-signed-url-replay-caveat)) — blocks whether Ingest-stage DLQ
  replay can work via resubmission at all, or must always re-poll Mercury. **Interacts with the
  categorization model:** an expired-URL dead-letter is `TRANSIENT` (redrive after re-poll), not
  `PERMANENT_BUSINESS` (see Assumption A-3) — it must never receive a fallback decision.
- Drain trigger mechanism (timer sweep vs. continuous peek-lock consumer per DLQ) — not specified;
  this doc assumes a timer for consistency with the other reliability functions.
- **Reason-code catalogue ownership** — the [reason codes](#reason-codes) here are a proposed
  starting set. Confirm with Intake/ops whether additional `PERMANENT_BUSINESS` reason codes are
  needed (each one is a distinct business situation an operator may need to filter on).
