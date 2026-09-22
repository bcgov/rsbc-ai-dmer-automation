# DLQ Drain

Source: architecture doc §4.6.3, §6. Data model: `../data-model.md`. Messages:
`../message-contracts.md`. See also [Reliability Components](09-reliability-components.md).

## Purpose

Reads every dead-letter sub-queue and converts each message into a **recorded, visible outcome**
rather than a silent loss. This is the mechanism that makes the fallback-outcome guarantee
(architecture document §8.3) concrete: "failure to decide must not mean failure to appear."

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
| Writes | `processing_error`, `dmer_document`, `dmer_decision`, `mercury_outbox` |

## Processing steps, per dead-lettered message

1. Write a `processing_error` row with `document_id`, `stage`, `error_class`, the dead-letter reason,
   and the original message id.
2. Set the document's `pipeline_status = MANUAL_REVIEW`.
3. Queue a **fallback outcome** to `mercury_outbox` — by default `IN`, with `decided_by = FALLBACK` —
   so the document still reaches Intake. Per question I-1 (answered): the Mercury payload's comment
   must say **"AI could not process"**, written back as part of the POST/PUT, not just logged. This
   is the step that makes failure visible rather than silent.
4. Raise an alert if the dead-letter rate crosses a threshold — a spike almost always means a
   systemic problem rather than a single bad document.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `processing_error` | `INSERT` | `document_id`, `stage`, `error_class` (`TRANSIENT`, `POISON`, or `DOWNSTREAM`), `message`, `dlq_message_id`, `occurred_at`. |
| `dmer_document` | `UPDATE` | `pipeline_status = MANUAL_REVIEW`. |
| `dmer_decision` | `INSERT` | `outcome_code = IN`, `decided_by = FALLBACK`, `decision_reason` naming the stage that failed. Subject to question I-1's confirmed answer above. |
| `mercury_outbox` | `INSERT` | `operation = UPDATE_OUTCOME` carrying the fallback outcome, so the document still reaches Intake. |

Note this inserts `dmer_decision` and `mercury_outbox` directly — unlike the normal path where only
[Post-Processing](08-post-processing.md) writes these tables. DLQ Drain is the one other writer,
and for the same reason Post-Processing uses a single transaction: **the fallback decision and the
intent to deliver it must commit together**, or the fallback path reintroduces the exact
invisible-DMER failure it exists to close.

## Pre-signed URL replay caveat

If a message dead-letters at [Ingest](01-ingest.md) and is replayed hours later, it may fail again
on an **expired pre-signed URL** (question M-1, open — see `01-ingest.md#failure-handling`). If the
TTL is short and no fresh-URL mechanism exists, DLQ Drain's remedy for an Ingest-stage dead letter
is **not** "resubmit the original message" but "re-poll that document through the Mercury batch API
to obtain a new URL." Build this branch from the start; do not discover the need during the first
incident.

## Interaction with upstream/downstream

Reads from every queue's DLQ across all stages. Its `mercury_outbox` writes are drained by the
[Outbox Publisher](09-reliability-components.md#outbox-publisher) exactly like a normal decision.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `SERVICEBUS_NAMESPACE` | Same namespace as the four main queues. |
| `DLQ_DRAIN_INTERVAL` | Timer or continuously-running peek-lock consumer per DLQ — not specified in the architecture document; timer-based (e.g. every 5 minutes) is simplest and matches the other reliability functions' pattern. |
| `DLQ_ALERT_THRESHOLD` | Dead-letter rate that triggers an alert — see [Azure Monitor](../services/azure-monitor.md). |
| `FALLBACK_OUTCOME_CODE` | Default `IN`. |

## Authentication / identity

Managed identity for Service Bus (dead-letter sub-queue receive across all four queues) and
PostgreSQL.

## Idempotency requirements

Draining the same dead-lettered message twice must not create two `processing_error` rows or two
fallback decisions for the same document — key the fallback-decision write on `document_id` with an
upsert/conflict-check, since a `dmer_decision` row may already exist for this document from a prior
drain attempt.

## Logging / auditing

Every drain is, by definition, a failure that reached the end of the line — log at WARN or ERROR
with `document_id`, `stage`, `error_class`, and the dead-letter reason string. This is the primary
input to "documents in `MANUAL_REVIEW` per day" (see [Azure Monitor](../services/azure-monitor.md)),
which is itself a model/input-quality regression signal (question I-16).

## Implementation considerations for Claude Code

- No placeholder folder exists for this function. Group it with the other reliability functions —
  see `09-reliability-components.md#implementation-considerations-for-claude-code`'s proposed
  `services/reliability/` app; DLQ Drain fits the same low-per-invocation, DB-and-queue-driven
  profile as the Outbox Publisher and Reconciliation Sweeper.
- Reading a Service Bus dead-letter sub-queue is a normal `ServiceBusReceiver` pointed at
  `<queue>/$DeadLetterQueue` — `libs/dmer_common/src/dmer_common/messaging/consumer.py`'s settlement
  logic (complete/dead-letter) doesn't directly apply here (there's no "re-dead-letter" path); this
  function completes each DLQ message after recording it, rather than routing it further.

## Example payload — fallback `mercury_outbox.payload`

```json
{
  "document_guid": "123e4567-e89b-...",
  "outcome_code": "IN",
  "comment": "AI could not process",
  "failed_stage": "EXTRACT",
  "duplicate_of": null
}
```

## Open Questions / Decisions Required

- **M-1** — pre-signed URL TTL / fresh-URL-on-demand availability (see
  [Pre-signed URL replay caveat](#pre-signed-url-replay-caveat)) — blocks whether Ingest-stage DLQ
  replay can work via resubmission at all, or must always re-poll Mercury.
- Drain trigger mechanism (timer sweep vs. continuous peek-lock consumer per DLQ) — not specified;
  this doc assumes a timer for consistency with the other reliability functions.
