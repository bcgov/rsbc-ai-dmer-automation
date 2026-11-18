# Azure Service Bus

Source: architecture doc §3.2, §5, §6. Bicep modules:
`infrastructure/bicep/modules/servicebus/{namespace,queue,topic}.bicep`. Full message contract and
queue topology: `../message-contracts.md` (this file covers the Azure resource/configuration side;
`message-contracts.md` covers the payload/contract side — don't duplicate between them).

## Tier

**Premium, 1 messaging unit.** Required for private endpoint support and predictable throughput —
Standard tier does not support private endpoints, which is a hard requirement (§9.1: private
endpoints for Blob, Postgres, Service Bus, and Document Intelligence, within the secured VNet).

## Entities

Four **queues** (see `../message-contracts.md#queues-vs-topics` for why queues rather than topics):
`dmer-ingest`, `dmer-raw`, `dmer-extracted`, `driver-decision`. `driver-decision` is the one entity
that needs **sessions enabled** at the Bicep/portal level (`requiresSession: true`) — this is not
optional configuration, it is what gives [Driver Orchestration](../stages/06-driver-orchestration.md)
its single-writer-per-driver guarantee when the team chooses the session-based join mechanism.

## Per-queue configuration

| Setting | Value | Applies to |
|---|---|---|
| Receive mode | PeekLock (never ReceiveAndDelete) | All four |
| `MaxDeliveryCount` | 5 | All four |
| Lock duration | 5 min, **with lock renewal** during long-running handlers | `dmer-raw` especially (extraction can take minutes) |
| Duplicate detection | Enabled, window sized to the poll interval | `dmer-ingest` only (`MessageId = document_guid`) |
| Sessions | Required, `SessionId = driver_key` | `driver-decision` only |
| Dead-lettering on message expiration | Enabled | All four |
| Time-to-live | Long enough that a downstream outage doesn't silently expire work | All four |

The reusable `infrastructure/bicep/modules/servicebus/queue.bicep` module should expose all of
these as parameters so the four queue instantiations in `main.bicep` differ only in name and the
session/duplicate-detection flags.

## Dead-letter queues

Every queue gets an automatic `<queue>/$DeadLetterQueue` sub-queue — nothing to provision. See
`../message-contracts.md#dead-letter-handling` for the classification rule (transient / downstream
outage / poison) and [DLQ Drain](../stages/10-dlq-drain.md) for the function that reads them. Three
operational facts worth restating here because they're infrastructure-level, not code-level:

- DLQs do not drain themselves and count against the entity's size quota — an undrained DLQ
  eventually blocks the main queue.
- There is no native requeue — draining means receive-then-send-a-new-message.
- A message that hits `MaxDeliveryCount` gets a system-generated reason, not an application one —
  always dead-letter known-bad input explicitly (`DeadLetterMessageAsync(reason, description)`).

## Naming

`kebab-case`. Queues: `<domain>-<stage>` (e.g. `dmer-ingest`, `driver-decision` — note the revised
architecture does not use the older `<domain>-<stage>-queue` suffix convention from
`docs/standards/naming-conventions.md`; see [Alignment gaps](#alignment-gaps-vs-current-code)).
Namespace: `sb-rsbc-dmer-shared-<env>-001` per
`docs/architecture/repository-design.md` §12.

## Monitoring

Alert on active message count (earliest indicator of a stalled stage) and dead-letter count (any
non-zero value, plus rate of change) per stage — see `azure-monitor.md`.

## Authentication / identity

Managed identity, RBAC roles scoped per queue: `Azure Service Bus Data Sender` on the producer
side, `Azure Service Bus Data Receiver` on the consumer side, granted to each Function
App/Container App's own managed identity — never a shared connection string.

## Security considerations

**Never put the licence number or clinical content in a message body.** Service Bus is encrypted
at rest, but any operator with portal access can peek a message — use `driver_key`, never
`licence_number`, in every payload (§9.2). This is enforced by the message shape itself (see
`../message-contracts.md`), not by a runtime filter — do not add fields to the envelope without
checking this rule first.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/messaging/{publisher,consumer}.py` are reusable for the three
  non-session queues (`dmer-ingest`, `dmer-raw`, `dmer-extracted`) with no changes to their
  settlement logic. `driver-decision` needs a session-aware receiver — neither current class
  supports `ServiceBusSessionReceiver`/session locks; this is new code, not an extension.
- `libs/dmer_common/src/dmer_common/messaging/idempotency.py`'s `IdempotencyStore` protocol is
  reusable; the in-memory implementation should be replaced with a PostgreSQL-backed one (the
  `dmer_document`/`dmer_stage_run` replay-guard pattern each stage doc describes largely
  supersedes needing a separate idempotency table, but the abstraction still fits for any consumer
  that doesn't have a natural DB row to check).

## Alignment gaps vs. current code

`docs/contracts/queues/{raw-dmer-queue,extracted-dmer-queue,dmer-lifecycle-events-topic}.md` and
`docs/standards/naming-conventions.md` describe the original architecture's two queues + one topic
(`raw-dmer-queue`, `extracted-dmer-queue`, `dmer-lifecycle-events`). The revised architecture has
**four queues, no topic**, with different names and a different message shape — see
`../message-contracts.md#alignment-gaps-vs-current-code`. `infrastructure/bicep/modules/servicebus/`
has no queue/topic instances defined yet (only the reusable `namespace.bicep`/`queue.bicep`/
`topic.bicep` modules exist, uninstantiated in `main.bicep`), so there is no infrastructure drift to
reconcile — just documentation and the `dmer_common` DTOs.
