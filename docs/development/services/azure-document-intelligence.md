# Azure Document Intelligence

Source: architecture doc §3.2, §4.2. Bicep module: `infrastructure/bicep/modules/ai/document-intelligence.bicep`.
Used exclusively by [Extraction](../stages/02-extraction.md).

## Role in the pipeline

Two distinct calls per document, both from the [DMER Extraction Processor](../stages/02-extraction.md):

1. **Custom-trained DMER model** — produces printed-content field/value JSON: checkbox states
   across sections A–G, the physician block, licence/DOB band, reason for examination, form
   identifiers. This is the deterministic half of extraction; trust it over anything a model infers
   later.
2. **`prebuilt-ocr`** — run over the handwriting regions identified by page segmentation (section D,
   visual acuity entries in B, follow-up interval in C, exam date/signature in F), returning raw
   text plus positional information per region. Its layout output is also what the cut-off-detection
   check reads (header/signature band presence) without needing an LLM call.

## Custom model training

**Manual, out of Bicep** — the custom model itself is trained/versioned outside the IaC pipeline
(per `docs/architecture/repository-design.md` §10). Record the model version used on every
`dmer_stage_run.model_version` for the `EXTRACT` stage (see `../data-model.md`) — a model retrain
changes extraction output, and disputes will turn on which version produced a given result.

## Throughput ceiling

Document Intelligence is **the throughput ceiling for the whole pipeline** (architecture doc §3.2).
The Extraction Container App's KEDA max-replica setting must be bounded by DI quota, not cost — see
`azure-container-apps.md`.

## Network

Private endpoint, inside the secured VNet (§9.1). No public network access.

## Failure handling

HTTP 429 is transient — retry inside the handler with exponential backoff; never abandon the
message on a 429 (abandoning consumes delivery count and will dead-letter a perfectly good document
during a busy period). See `../message-contracts.md#dead-letter-handling` for the full
transient/poison classification.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `DOCUMENT_INTELLIGENCE_ENDPOINT` | Private endpoint URL. |
| `DI_CUSTOM_MODEL_ID` | Custom DMER model identifier/version. |

## Authentication / identity

Managed identity (`Cognitive Services User` role), reached over the private endpoint — no API key.

## Monitoring

Alert on 429 rate > 1% over 15 minutes (quota pressure) and on stage duration p50/p95 from
`dmer_stage_run` (capacity planning) — see `azure-monitor.md`.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/doc_intelligence/client.py` (`DocumentIntelligenceClient`,
  `DIResult.from_sdk`) is reusable as-is — it already wraps `begin_analyze_document`/`poller.result()`
  with retry (`di_retry`: 4 attempts, 1–15s backoff) and a circuit breaker (`di_breaker`: 5-failure
  threshold, 30s reset), authenticated via `DefaultAzureCredential`.
- The client's `analyze(model_id, document, *, pages=None)` signature already supports calling it
  once with the custom model and once with `prebuilt-ocr` on a page subset — no new client code
  needed, just two call sites from the Extraction processor.
- Page segmentation (splitting the document into handwriting regions before the `prebuilt-ocr` call)
  has no existing implementation anywhere in the repo — this is new code within
  `services/di-processor/`.
