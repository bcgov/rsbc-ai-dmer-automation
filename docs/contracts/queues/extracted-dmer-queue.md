
# `extracted-dmer-queue`

> **Superseded.** See
> [`docs/development/message-contracts.md`](../../development/message-contracts.md) for the
> revised architecture's four queues and shared message shape. This queue's nearest equivalent is
> `dmer-extracted` (payload shape differs — see that doc).

| | |
|---|---|
| Type | Service Bus Queue |
| Producer | `di-processor` |
| Consumer | `workflow-orchestrator` (orchestration trigger) |
| Max delivery count | 5 |
| Lock duration | 5 min |
| Dead-letter | Native `$DeadLetterQueue` |

## Message envelope

```json
{
  "messageId": "uuid",
  "correlationId": "uuid-or-mercury-case-id",
  "schemaVersion": "1.0",
  "documentId": "string",
  "mercuryCaseId": "string",
  "sha256Hash": "hex string",
  "ocrResultUri": "blob path under ocr/",
  "processedAt": "2026-08-05T12:05:00Z"
}
```
