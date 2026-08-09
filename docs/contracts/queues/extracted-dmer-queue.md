
# `extracted-dmer-queue`

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
