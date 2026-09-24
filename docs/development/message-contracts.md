# Message Contracts & Queue Topology

Canonical reference for every Service Bus entity in the revised architecture
(`docs/architecture/DMER_Intake_Automation_Revised_Architecture.docx`, §5–§6). Every stage doc
cross-references this file instead of restating queue config or the message shape.

**This supersedes** `docs/contracts/queues/raw-dmer-queue.md`, `extracted-dmer-queue.md`, and
`dmer-lifecycle-events-topic.md`, and the `RawDmerMessage` / `ExtractedDmerMessage` DTOs in
`libs/dmer_common/src/dmer_common/dto/messages.py`. See
[Alignment gaps](#alignment-gaps-vs-current-code) below — the queue names, count, and message shape
all changed.

## Queue topology

Four Service Bus **queues** (not topics — see [Queues vs. topics](#queues-vs-topics)):

| Queue | Producer | Consumer | Configuration |
|---|---|---|---|
| `dmer-ingest` | Page Poller, Webhook Listener | Ingest Function | Duplicate detection enabled, window sized to the poll interval; `MessageId = document_guid`. |
| `dmer-raw` | Ingest Function | DMER Extraction Processor (Container App) | Lock duration 5 minutes **with lock renewal during extraction**; `MaxDeliveryCount` 5. Named `dmer-raw` — not `dmer-extract` — because it carries documents that are raw and awaiting extraction (see [Naming note](#naming-note-dmer-raw-not-dmer-extract)). |
| `dmer-extracted` | DMER Extraction Processor | Document Orchestrator (starts the durable orchestration) | `MaxDeliveryCount` 5. |
| `driver-decision` | Document Orchestrator | Driver Orchestrator (starts the durable orchestration) | **Sessions required**; `SessionId = driver_key`. `MaxDeliveryCount` 5. This is what gives single-writer-per-driver semantics — see [Driver Orchestration](stages/06-driver-orchestration.md). |

Applies to all four: PeekLock (never ReceiveAndDelete), dead-lettering on message expiration
enabled, time-to-live long enough that a downstream outage does not silently expire work. Every
queue has an automatic dead-letter sub-queue at `<queue>/$DeadLetterQueue` — see
[Dead-letter handling](#dead-letter-handling) and [DLQ Drain](stages/10-dlq-drain.md).

### Queues vs. topics

Use queues unless a second independent subscriber exists **today**. A topic with one subscription
is a queue with extra configuration. The one exception worth planning for: `dmer-raw` is the queue
most likely to gain a second subscriber (a metrics/audit consumer) — if that becomes concrete,
convert it to a topic rather than bolting a second reader onto the existing queue.

### Naming note: `dmer-raw`, not `dmer-extract`

The queue between Ingest and Extraction is `dmer-raw`. The original architecture named it
`dmer-extract`, which read as the opposite of what it holds (documents *awaiting* extraction) and
sat confusingly close to `dmer-extracted` (documents that *have been* extracted). If you see
`dmer-extract` anywhere (old diagrams, old branches), it means the same queue as `dmer-raw`.

## Message envelope

One shape for all four queues — pointers only, never extracted/normalized content:

```json
{
  "schema_version": "1.0",
  "message_id":     "5893ac38-40b3-5070-...",
  "document_id":    "8f3c1b2a-...",
  "document_guid":  "123e4567-e89b-...",
  "driver_key":     "a91b77e4-...",
  "blob_url":       "https://.../extracted-dmer/8f3c1b2a/combined.json",
  "correlation_id": "5d10...",
  "attempt":        1,
  "enqueued_at":    "2026-09-18T12:00:00Z"
}
```

| Field | Notes |
|---|---|
| `message_id` | Identifies **this** event on **this** queue, and is the idempotency key. Derive it deterministically from the event — `dmer_common.dto.event_message_id(<queue>, <natural key>)`, e.g. `event_message_id("dmer-extracted", document_id)` — so every retry or replay of the same event carries the same ID. **Never copy the upstream message's `message_id`**: two different events would then share an ID, and a consumer could mistake one for the other (or fail to recognise a replay triggered by a re-sent upstream message). `correlation_id` is what links events across stages. Also used as the Service Bus `MessageId` (broker duplicate detection, per queue). The `dmer-ingest` exception: `MessageId = document_guid`, for broker duplicate detection of re-polled documents. |
| `document_id` | Internal `dmer_document.id` (uuid) — not `document_guid`. Use this for every DB join and log line. |
| `document_guid` | Mercury's identifier. Carried for traceability; **do not** use it as a business key downstream of Ingest (see `data-model.md#document_guid-is-not-a-content-key`). |
| `driver_key` | Set only when Mercury supplied it at Ingest; otherwise resolved by [Document Orchestration's Resolve Driver activity](stages/03-document-orchestration.md#activity-resolve-driver) before `driver-decision` is published; Extraction forwards it as received. Required on `driver-decision`. |
| `blob_url` | Points at the artifact the *next* stage needs — `raw-dmer` for `dmer-ingest`→Ingest's own read, `extracted-dmer` for `dmer-extracted`, etc. Never an extraction/normalization payload inline. |
| `attempt` | Incremented on republish (sweeper re-signal, DLQ Drain fallback requeue). |

**Security**: never put the licence number or any clinical content in a message body — Service Bus
is encrypted at rest, but any operator with portal access can peek a message. Use `driver_key`. The
same rule applies to Application Insights traces (§9.2).

### `mercury_outbox` operations (not a queue message — a DB-driven payload)

The Outbox Publisher reads `mercury_outbox.payload` (jsonb), not a queue message. Included here
because it's the other message-shaped contract in the system:

| Operation | Purpose |
|---|---|
| `UPDATE_OUTCOME` | Write the outcome code (`CP`/`IN`/`PR`/`PU`/`PCM`/`CR`) and reason back to the DMER record. |
| `MARK_DUPLICATE` | Flag a document as a duplicate of another — Mercury's `Rejected` status per question I-7. |
| `MAP_DRIVER` | Attach the proposed driver to the DMER record (question I-11: AI may do this automatically). |
| `CREATE_CASE` | Create a case where none exists, or attach to an existing open case (question I-13). |

See [Post-Processing](stages/08-post-processing.md) for how these rows are written and
`azure-service-bus.md`/`reliability-components.md` for how they're drained.

## Message idempotency (consumer side)

Every consumer uses `dmer_common.messaging.ServiceBusConsumer` with a **durable** idempotency store
(`PostgresIdempotencyStore`, table [`message_idempotency`](data-model.md#message_idempotency)).
There is no in-memory default: `InMemoryIdempotencyStore` exists for unit tests only, because it
forgets everything on restart and is invisible to other replicas.

The decision to run a handler is **one atomic claim**, never `is_processed()` → handler →
`mark_processed()` (which lets two workers both see "not processed" and both run):

1. **Claim** — `INSERT ... ON CONFLICT (scope, message_id) DO UPDATE ... WHERE status =
   'PROCESSING' AND lease_expires_at < now()`. The primary key `(scope, message_id)` guarantees
   exactly one worker gets the claim (a fresh one, or a takeover of an expired one).
2. **Only the claimer runs the handler.**
3. **Success** → the claim becomes `COMPLETED` (`processed_at` set), then the message is completed.
   A message is never marked completed before its handler succeeds.
4. **Failure** → the claim is released (deleted), then the message is dead-lettered, so it stays
   eligible for retry / redrive.

| Claim outcome | Consumer action |
|---|---|
| `CLAIMED` | Run the handler (then complete, or release + dead-letter) |
| `DUPLICATE` — already `COMPLETED` | Complete the message without running the handler |
| `IN_PROGRESS` — another worker holds a live claim | **Abandon** the message so Service Bus redelivers it later. Never complete it: if that worker has crashed, completing would lose the message |
| The claim itself fails (store unavailable) | Leave the message unsettled; its lock expires and it is redelivered |

Only the claim's holder can complete or release it (a per-claim `claim_token`), so a worker whose
claim was taken over can't overwrite the new holder. `scope` names the consumer (e.g.
`di-processor/dmer-raw`), so one consumer's completed ID never suppresses another's.

**Lease:** 5 minutes by default, matching the `dmer-raw` lock duration — by the time Service Bus
redelivers after a lost lock, a crashed worker's claim is also takeable. Lease times use the
database clock.

Behaviour under failure:

| Situation | Outcome |
|---|---|
| Crash before the handler (claim taken) | The claim stays `PROCESSING` until its lease expires; the redelivery then takes it over and runs the handler |
| Crash during the handler | Same. Partial work is safe to redo (status compare-and-set, overwritable blob paths, deterministic outbound message IDs) |
| Crash after the handler, before the claim is marked `COMPLETED` | Reprocessed after the lease expires; the handler's own idempotency makes the rerun a no-op or a re-publish of the **same** event ID |
| Crash after `COMPLETED`, before the message is completed | The redelivery sees `DUPLICATE` and completes it without running the handler |
| Duplicate Service Bus delivery | `DUPLICATE` — suppressed durably, across restarts and replicas |
| Two replicas receive the same message | Only after a lock expiry; exactly one claims it, the other abandons it |
| A long run outlives its lease (no lock renewal yet) | A second worker can take the claim over and run concurrently; the status compare-and-set lets exactly one finish, at the cost of repeated DI/LLM calls. Renewing the lease with the lock would remove this |

**Guarantee:** at-least-once handling with **effectively-once side effects** where the handler is
idempotent. This is **not** exactly-once: a handler can still run twice in the rows above, so
every handler must stay idempotent. The store makes duplicate runs rare and suppresses every
duplicate delivery of a completed message.

## Dead-letter handling

| Cause | How it happens |
|---|---|
| Delivery count exceeded | Handler throws/abandons/crashes, or a lock expires mid-processing (counts as a delivery) — redelivered until `MaxDeliveryCount` (5) is reached. |
| Time-to-live expiry | Only when `EnableDeadLetteringOnMessageExpiration` is set (it is, on all four queues). |
| Explicit dead-lettering | Handler calls `DeadLetterMessageAsync(reason, description)` — the only path that records *why*. Always prefer this for known-bad input. The shared `dmer_common.messaging.ServiceBusConsumer` does this on any handler exception: `reason`/`description` come from the exception's `dead_letter_reason`/`safe_detail` attributes when present (e.g. extraction's failure codes), otherwise `HandlerError` and the exception type. It never logs or sends the exception message (PII risk). |

Classify every failure explicitly:

| Class | Examples | Handling |
|---|---|---|
| **Transient** | HTTP 429 from DI/OpenAI, Mercury timeout, transient DB error | Retry inside the handler with exponential backoff. Never let it consume delivery count on its own — only dead-letter after the internal retry budget is exhausted. |
| **Downstream outage** | Mercury unavailable, Azure OpenAI region issue | Back off and let the queue build; the queue is the shock absorber. Alert on queue depth, not individual failures. |
| **Poison** | Malformed/unreadable PDF, `document_guid` that no longer exists, schema violation | Dead-letter immediately and explicitly with a reason. Retrying wastes quota and delays everything behind it in the queue. |

Dead-letter queues do not drain themselves and there is no native requeue — see
[DLQ Drain](stages/10-dlq-drain.md) for the function that reads every `$DeadLetterQueue` and turns
each message into a recorded, visible outcome.

## Alignment gaps vs. current code

`libs/dmer_common/src/dmer_common/dto/messages.py` currently defines `RawDmerMessage`
(`raw-dmer-queue`) and `ExtractedDmerMessage` (`extracted-dmer-queue`, v2, with
`combined_result_uri`/`sha256_hash`) — the original architecture's two-queue, seven-service shape.
Under the revised architecture:

- There are **four** queues, not two, with the names above (`dmer-ingest`, `dmer-raw`,
  `dmer-extracted`, `driver-decision`), and a fifth logical delivery path (`mercury_outbox`, DB-driven).
- One message shape covers all four queues (`document_id`, `document_guid`, `driver_key`,
  `blob_url`, `correlation_id`, `attempt`, `enqueued_at`) rather than a distinct DTO per queue.
- `driver-decision` requires **sessions** (`SessionId = driver_key`) — the current
  `ServiceBusPublisher`/`ServiceBusConsumer` in `libs/dmer_common/src/dmer_common/messaging/` have
  no session-aware send/receive path yet; a session receiver (`ServiceBusSessionReceiver`, or the
  Durable Functions Service Bus session trigger) is new work, not an extension of the existing
  consumer.

**What's reusable as-is:** the `Envelope` base class's camelCase-on-the-wire pattern
(`message_id`/`correlation_id`/`schema_version`), the `ServiceBusPublisher`/`ServiceBusConsumer`
settlement logic (complete on success, dead-letter with reason on handler failure, no-op on a
`message_id` already processed), and the idempotency store abstraction — keyed on
`(idempotency_scope, message_id)`, where the scope names the consumer (e.g.
`di-processor/dmer-raw`), so a store shared between services can never let one consumer's
completed ID suppress another's; a durable implementation keys its table the same way. These are
architecture-agnostic and should be kept; only the concrete DTOs and the queue names they map to
need to change. Add `IngestMessage` (or rename `RawDmerMessage`), `RawMessage`, `ExtractedMessage`,
and `DriverDecisionMessage` (session-aware) as the four envelope subclasses.
