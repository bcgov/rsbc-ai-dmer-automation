
# `dmer-lifecycle-events` (Topic)

> **Superseded.** The revised architecture has no lifecycle-events topic — post-processing is now
> an in-process Durable activity rather than a topic subscriber. See
> [`docs/development/message-contracts.md`](../../development/message-contracts.md) and
> [`docs/development/stages/08-post-processing.md`](../../development/stages/08-post-processing.md).

| | |
|---|---|
| Type | Service Bus Topic |
| Producer | `workflow-orchestrator` |
| Subscriptions | `sub-post-processing` (post-processing), `sub-audit-service` (audit-service, optional) |
| Dead-letter | Per-subscription native `$DeadLetterQueue` |

## Message envelope

```json
{
  "messageId": "uuid",
  "correlationId": "uuid-or-mercury-case-id",
  "schemaVersion": "1.0",
  "documentId": "string",
  "mercuryCaseId": "string",
  "eventType": "completed | failed",
  "ruleVersion": "string",
  "decision": "string (null if failed)",
  "reasonCodes": ["string"],
  "failureReason": "string (null if completed)",
  "occurredAt": "2026-08-05T12:07:00Z"
}
```

Using a topic (rather than a direct call from the orchestrator) lets `post-processing` and
`audit-service` — and any future subscriber, e.g. a notification service — consume the same
lifecycle event independently.
