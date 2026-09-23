
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

## Message envelope (v2)

```json
{
  "messageId": "uuid",
  "correlationId": "uuid-or-mercury-case-id",
  "schemaVersion": "2.0",
  "documentId": "string",
  "mercuryCaseId": "string",
  "sha256Hash": "hex string",
  "combinedResultUri": "blob path under combined-extracted-dmer/",
  "processedAt": "2026-08-05T12:05:00Z"
}
```

`messageId` is the idempotency key. The single source of truth for this schema is
`dmer_common.dto.ExtractedDmerMessage`.

## v2 changes (breaking)

`di-processor` now performs a multi-stage extraction (custom-model top-level fields + LLM
handwritten reconstruction, merged) and references the **combined** extraction result rather than
a raw OCR result.

- `ocrResultUri` (v1) → `combinedResultUri` (v2). The URI points at the unified combined
  extraction JSON under the `combined-extracted-dmer/` container.
- `schemaVersion` bumped `1.0` → `2.0` (field rename is breaking).
- `sha256Hash` retained (source-document dedupe).

`workflow-orchestrator` is the sole consumer and must read `combinedResultUri` to locate the
extraction result for the next pipeline stage.
