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
  "document_id":    "8f3c1b2a-...",
  "document_guid":  "123e4567-e89b-...",
  "driver_key":     "a91b77e4-...",
  "blob_url":       "https://.../extracted-dmer/8f3c1b2a.json",
  "attempt":        1,
  "enqueued_at":    "2026-09-18T12:00:00Z"
}
```

| Field | Notes |
|---|---|
| `document_id` | Internal `dmer_document.id` (uuid) — not `document_guid`. Use this for every DB join and log line; also the tracing key across a document's whole life — there is no separate `correlation_id`. |
| `document_guid` | Mercury's identifier. Carried for traceability; **do not** use it as a business key downstream of Ingest (see `data-model.md#document_guid-is-not-a-content-key`). |
| `driver_key` | Null until Extraction resolves it (or Mercury supplied it at Ingest). Required on `driver-decision`. |
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

## Dead-letter handling

| Cause | How it happens |
|---|---|
| Delivery count exceeded | Handler throws/abandons/crashes, or a lock expires mid-processing (counts as a delivery) — redelivered until `MaxDeliveryCount` (5) is reached. |
| Time-to-live expiry | Only when `EnableDeadLetteringOnMessageExpiration` is set (it is, on all four queues). |
| Explicit dead-lettering | Handler calls `DeadLetterMessageAsync(reason, description)` — the only path that records *why*. Always prefer this for known-bad input. |

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
  `blob_url`, `attempt`, `enqueued_at`) rather than a distinct DTO per queue.
- `driver-decision` requires **sessions** (`SessionId = driver_key`) — the current
  `ServiceBusPublisher`/`ServiceBusConsumer` in `libs/dmer_common/src/dmer_common/messaging/` have
  no session-aware send/receive path yet; a session receiver (`ServiceBusSessionReceiver`, or the
  Durable Functions Service Bus session trigger) is new work, not an extension of the existing
  consumer.

**Already done:** the `Envelope` base class now carries `message_id`/`document_id`/`schema_version`
(no separate `correlation_id` — `document_id` serves that role, per the correlation-id decision
above), and `RawDmerMessage`/`ExtractedDmerMessage` no longer redeclare `document_id` themselves
since it's inherited from `Envelope`. `dmer_common.telemetry`'s context-propagation helpers
(`document_id_context`/`get_document_id`) and `ServiceBusPublisher`/`ServiceBusConsumer` were
updated to match.

**What's reusable as-is, still pending the queue-specific rework:** the `Envelope` base class's
camelCase-on-the-wire pattern, and the `ServiceBusPublisher`/`ServiceBusConsumer` settlement logic
(complete on success, dead-letter with reason on handler failure, no-op on a `message_id` already
processed) and idempotency store abstraction — all architecture-agnostic and already kept. Still to
add: `IngestMessage` (or rename `RawDmerMessage`), `RawMessage`, `ExtractedMessage`, and
`DriverDecisionMessage` (session-aware) as the four envelope subclasses for the new queue names.
