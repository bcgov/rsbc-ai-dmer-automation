
# `raw-dmer-queue`

| | |
|---|---|
| Type | Service Bus Queue |
| Producer | `intake-processor` |
| Consumer | `di-processor` |
| Max delivery count | 5 |
| Lock duration | 5 min |
| Duplicate detection window | 10 min (on `messageId`) |
| Dead-letter | Native `$DeadLetterQueue`; monitored by the `dlq-depth` alert (see `docs/operations/monitoring-alerts.md`) |

## Message envelope

```json
{
  "messageId": "uuid",
  "correlationId": "uuid-or-mercury-case-id",
  "schemaVersion": "1.0",
  "sourceSystem": "mercury-batch | mercury-webhook",
  "documentId": "string",
  "mercuryCaseId": "string",
  "documentUri": "s3://... or https://...(pre-signed)",
  "receivedAt": "2026-08-05T12:00:00Z",
  "payload": {
    "...": "source-specific metadata"
  }
}
```

`messageId` is the idempotency key — `di-processor` must no-op (not error) on a duplicate
`messageId` it has already completed.
