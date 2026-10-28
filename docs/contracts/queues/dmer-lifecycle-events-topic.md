
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

## Failed-event semantics (aligns with the categorized-failure model)

Even though this topic is superseded, if it is ever revived its `failed` event must respect the same
safety rule the revised [DLQ Drain](../../development/stages/10-dlq-drain.md#failure-categorization-the-first-thing-the-drain-does)
enforces: **a `failed` event is a notification, not a decision.** Specifically:

- A `failed` event carries `decision: null`. It must **never** be interpreted by any subscriber as a
  business outcome (e.g. it does not mean the DMER is `IN`).
- Add a `failureCategory` field (`PERMANENT_BUSINESS | TRANSIENT | PROCESSING | UNKNOWN`) and use
  `reasonCodes` for the stable reason codes defined in
  [DLQ Drain §Reason codes](../../development/stages/10-dlq-drain.md#reason-codes), so a subscriber
  can distinguish a permanent-business failure from a transient/infrastructure one without parsing
  free text.
- Only the DLQ Drain (for a `PERMANENT_BUSINESS` failure, under the explicit business rule) may turn
  a failure into a fallback `IN` decision. No subscriber to this topic may synthesize a business
  decision from a `failed` event — doing so would let a transient fault masquerade as a considered
  outcome, the exact failure the revised design prevents.
