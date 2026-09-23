# Stage 2 — Extraction

Source: architecture doc §4.2. Figure: `docs/architecture/Figure1_Revised_Architecture.png`
("Stage 2 — Extraction, the only container app"). Data model: `../data-model.md`. Messages:
`../message-contracts.md`.

## Purpose

Turn a scanned page into structured data, and record the licence number as read from the page.
Establishing which driver the document belongs to is **not** done here (see
[Driver resolution is not performed in Extraction](#driver-resolution-is-not-performed-in-extraction)).
This is **the only Container App in the design**. It earns that because it
runs for tens of seconds to minutes per document, loads models, splits images, and scales on a
completely different curve from the rest of the pipeline (bounded by Document Intelligence/OpenAI
quota, not by cost). See `../services/azure-container-apps.md` for the KEDA scale rule.

## Trigger / reads / writes

| | |
|---|---|
| Type | Azure Container App, Service Bus queue consumer on `dmer-raw`, KEDA scale rule on queue depth |
| Reads | `raw-dmer` blob; Document Intelligence custom model + `prebuilt-ocr`; Azure OpenAI GPT-5.1 |
| Writes | `extracted-dmer` blob, `dmer_extraction`, `dmer_document`, `dmer_stage_run` |
| Publishes | `dmer-extracted` |
| Concurrency | Max replicas bounded by Document Intelligence and Azure OpenAI quota, not cost |

## Processing steps, in order

1. **Replay guard and download.** If `pipeline_status` is already `EXTRACTED` or later, complete
   the message and return. Otherwise download the PDF from `raw-dmer`.
2. **Custom Document Intelligence model.** Produces the printed content as field/value JSON: the
   checkbox states across sections A–G, the physician block, the licence/DOB band, the reason for
   examination, and form identifiers. This is the deterministic half of extraction — trust it over
   anything a model infers later.
3. **Page segmentation.** Split the document into the regions that carry handwriting: principally
   section D (details of conditions), the visual acuity entries in section B, the follow-up
   interval in section C, and the examination date/signature in section F. Segmenting first and
   running OCR on smaller regions materially improves handwriting accuracy versus a single
   full-page pass.
4. **`prebuilt-ocr` over the handwriting regions.** Returns raw text plus positional information,
   per region.
5. **GPT-5.1 structuring.** Send the OCR output, region by region, with the target schema, to
   obtain structured JSON for the handwritten content. **Keep the prompt and schema versioned and
   record the version on the stage run** — a prompt change alters output, and future disputes will
   turn on which version produced a given result.
6. **Cut-off detection.** Determine whether the top and bottom bands of the form survived the
   fax/scan. Persist **three separate booleans** — `has_header`, `has_signature`, `is_cutoff` —
   rather than one collapsed flag, because Intake needs to know which half was missing.
7. **Licence read.** Record the licence number as read from the page on
   `dmer_extraction.licence_number_read` (canonical 8-digit form). This step only *records* the
   read — it does not resolve a `driver`, attach a `driver_key`, or touch `driver_evaluation`.
8. **Combine and hash.** Merge the custom-model result and the handwriting result into one JSON,
   write it to `extracted-dmer`, and compute the canonical comparison hash used later for duplicate
   detection.
9. **Publish.** Send to `dmer-extracted` with `document_id`, the `driver_key` as received on
   `dmer-raw` (null when Mercury supplied none), and the extracted blob URL.

### Cut-off detection in practice

On the MV2011C form, the top band carries the fax banner and the heading "DRIVER'S MEDICAL
EXAMINATION"; the bottom band carries the physician signature block, clinic address, and the
MV2011C form footer. Both are printed elements at fixed relative positions, so presence can be
determined from the `prebuilt-ocr` layout output **without an LLM call**. Treat the page as cut off
when **either** band is absent. Run this check before the expensive structuring call if you want to
save quota — but still extract the content, because a cut-off document's fields are needed for
duplicate comparison even when its outcome is superseded (see
[Decision Gateway](07-decision-gateway.md), step 2).

> **As implemented** (`services/di-processor/src/di_processor/extraction/cutoff.py`): the check
> reads the **custom-model** call's page layout, not the tiled `prebuilt-read` output — the tiled
> path runs on an image with the top Protected B band cropped off, so it can never see the header.
> A band is present when ≥ 2 of its printed labels fuzzy-match (≥ 0.80) inside its expected
> vertical band (header: top 20%; examiner/signature block: bottom 40%). The bottom band is also
> present if the model *located* one of its fields (`doctor_signature`, `medical_examination_date`,
> `physician_or_np_fax_present`) — located means a value or bounding region; an absent field still
> carries a high confidence, so confidence is ignored. The fax-machine banner is not an anchor. No
> page layout → all three flags `null` (undeterminable). Flags are written to `dmer_extraction` and
> to `combined.json` (`cutoff` section, with the matched evidence).

### Driver resolution is not performed in Extraction

**Decided (2026-09-23):** Extraction does **not** do driver identification (resolving/creating the
`driver` row and attaching `driver_key` when Mercury supplied none) or document counting (creating
or attaching `driver_evaluation` and setting `expected_document_count`). This reverses the
architecture document's placement of driver resolution in this stage.

**Now owned by** [Document Orchestration's Resolve Driver activity](03-document-orchestration.md#activity-resolve-driver)
(the first activity, so it completes before `driver-decision`, which requires `driver_key`, is
published).

What Extraction provides for it: `dmer_extraction.licence_number_read`, normalized with
`dmer_common.licence.normalize_licence` so it matches `driver.licence_number` exactly.
`dmer-extracted` carries `driver_key` only when Ingest set it from Mercury; otherwise it is null.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_extraction` | `INSERT` (one row per document) | `document_id`, `licence_number_read`, `exam_date`, `physician_name`, `has_header`, `has_signature`, `is_cutoff`, `page_count`, `confidence_avg`, `comparison_fields` (jsonb, canonicalized subset), `comparison_hash` (sha256 of the canonicalized subset). |
| `dmer_document` | `UPDATE` | `pipeline_status = EXTRACTED`, `current_stage = NORMALIZE`, `updated_at`. |
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = EXTRACT`, `status`, `attempt_no`, `started_at`, `ended_at`, `output_blob_url` = the combined extraction blob, `model_version` (custom DI model version **and** the GPT prompt/schema version — record both), error fields on failure. |

## Blob writes

**Decided:** four separate files per document, under a `document_id` path in `extracted-dmer`:

```
extracted-dmer/<document_id>/top_level.json    custom-model fields (+ cut-off flags)
extracted-dmer/<document_id>/ocr.json          tiled prebuilt-read OCR
extracted-dmer/<document_id>/handwritten.json  LLM-reconstructed handwritten fields
extracted-dmer/<document_id>/combined.json     merged result — the one downstream reads
```

`combined.json` is self-contained (merged fields, cut-off flags, model and prompt versions), so a
replay of normalization still needs a single fetch; the `dmer-extracted` message's `blob_url` and
`dmer_stage_run.output_blob_url` point at it. The other three are intermediate artifacts kept for
audit and for re-running a later sub-step without repeating earlier ones. Built by
`libs/dmer_common/src/dmer_common/storage/paths.py`.

## Failure handling

- HTTP 429 from Document Intelligence or Azure OpenAI is **transient**: retry inside the handler
  with exponential backoff, never by abandoning the message — abandoning consumes delivery count
  and will dead-letter a perfectly good document during a busy period.
- An unreadable or malformed PDF is **poison**: dead-letter it explicitly with a reason rather than
  retrying five times.
- **Renew the Service Bus lock while extraction runs**, or a slow document will be redelivered
  while it is still being processed (lock duration on `dmer-raw` is 5 minutes; extraction can take
  minutes).

### Failure codes

Every failure is reported by **which step failed**
(`services/di-processor/src/di_processor/failures.py`). The same code goes to
`dmer_stage_run.error_code`, the Service Bus dead-letter `reason`, and the error log; the detail
goes to `dmer_stage_run.error_detail` and the dead-letter `description`. On any failure the
document still goes to `MANUAL_REVIEW` and the message is dead-lettered.

| Code | Step |
|---|---|
| `DB_READ_FAILED` | Reading the document's status or stored pointer |
| `DB_WRITE_FAILED` | Status writes, stage-run start, `dmer_extraction` write |
| `INVALID_STATUS_TRANSITION` | A status change the state machine forbids (e.g. a `MANUAL_REVIEW` document redelivered) |
| `SOURCE_DOWNLOAD_FAILED` | Downloading the source PDF |
| `PDF_UNREADABLE` | Rendering page 1 (missing page, corrupt PDF) |
| `DI_CUSTOM_MODEL_FAILED` | Custom-model analyze (Stage A) |
| `OCR_FAILED` | Tiled OCR — only when **every** tile fails; individual failed tiles are skipped and counted in the log |
| `LLM_CALL_FAILED` | The Azure OpenAI call (Stage C) |
| `LLM_OUTPUT_INVALID` | Unparseable or schema-violating LLM output (Stage C) |
| `ARTIFACT_WRITE_FAILED` | Uploading any of the four extraction files |
| `PUBLISH_FAILED` | Publishing to `dmer-extracted` |
| `EXTRACTED_POINTER_MISSING` | Re-publish replay found an `EXTRACTED` document with no stored blob URL |
| `UNEXPECTED` | Anything else (a code bug, e.g. in merge) |

The detail is `key=value; ...` text built only from safe facts, e.g.
`error=ResourceNotFoundError; http_status=404`, `error=CircuitOpenError; circuit_open=true`,
`error=AllTilesFailed; failed_tiles=12; tiles=12`. **The exception message is never recorded or
logged** — it can carry extracted licence or clinical values (a schema-validation error echoes the
offending field values), and key-based log redaction cannot catch it.

## Interaction with upstream/downstream stages

Consumes `dmer-raw` (from [Ingest](01-ingest.md)). Publishes `dmer-extracted`, consumed by
[Document Orchestration](03-document-orchestration.md). Does not create or update `driver` or
`driver_evaluation` (see [above](#driver-resolution-is-not-performed-in-extraction)).

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `DOCUMENT_INTELLIGENCE_ENDPOINT` | Private endpoint URL. |
| `DI_CUSTOM_MODEL_ID` | Custom DMER model version — record on `dmer_stage_run.model_version`. |
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` / `AZURE_OPENAI_API_VERSION` | See `../services/azure-openai.md`; sourced via `dmer_common.config.openai_settings()`. |
| `EXTRACTED_DMER_CONTAINER` | Default `extracted-dmer`. |
| `KEDA_MAX_REPLICAS` | Bounded by DI/OpenAI quota — see `../services/azure-container-apps.md`. |
| `SERVICEBUS_NAMESPACE`, `DMER_RAW_QUEUE`, `DMER_EXTRACTED_QUEUE` | See `../message-contracts.md`. |

## Authentication / identity

Managed identity for Document Intelligence (private endpoint), Blob, Service Bus, PostgreSQL.
Azure OpenAI is the one documented exception — key-based auth via Key Vault secret, because the
GPT-5.1 deployment is hosted in a separate AI Hub subscription (see `../services/azure-openai.md`).

## Idempotency requirements

Replay guard on `pipeline_status >= EXTRACTED` (step 1). Re-running extraction for an already
`EXTRACTED` document (e.g. a redelivered message after a lock-renewal failure) must not create a
duplicate `dmer_extraction` row — use `document_id` as the natural upsert key on `dmer_extraction`.

## Logging / auditing

No licence numbers or clinical content in logs (§9.2) — log `document_id`, `driver_key`,
confidence scores, and stage timings only. Record `model_version` on every `dmer_stage_run` row so
a disputed extraction can be traced to the exact model/prompt combination that produced it.

## Example payload — `dmer-extracted` message

```json
{
  "schema_version": "1.0",
  "document_id": "8f3c1b2a-...",
  "document_guid": "123e4567-e89b-...",
  "driver_key": "a91b77e4-...",
  "blob_url": "https://.../extracted-dmer/8f3c1b2a/combined.json",
  "correlation_id": "5d10...",
  "attempt": 1,
  "enqueued_at": "2026-09-18T12:05:00Z"
}
```

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/doc_intelligence/client.py` (`DocumentIntelligenceClient`,
  `analyze()`) and `openai_client/client.py` (`OpenAIClient.complete()`) are reusable as-is — both
  already wrap retry + circuit breaker (`retry/policies.py`: `di_retry`/`di_breaker`,
  `openai_retry`/`openai_breaker`) matching the failure-handling rules above.
- `libs/dmer_common/src/dmer_common/storage/containers.py` uses the single `extracted-dmer`
  container and `paths.py` builds the four per-document paths — matches the
  [Blob writes](#blob-writes) layout.
- `services/di-processor/` is the placeholder folder for this stage (currently
  `main() -> raise NotImplementedError`) — this is where the Container App entrypoint, KEDA scaling
  config, and the nine processing steps above belong.
- `dmer_document`, `dmer_extraction`, and `dmer_stage_run` repositories exist in
  `libs/dmer_common/src/dmer_common/db/`. No `driver`/`driver_evaluation` repository is needed in
  this stage.

## Open Questions / Decisions Required

- ~~Owner of driver resolution and document counting~~ — **resolved 2026-09-23**: Document
  Orchestration's [Resolve Driver activity](03-document-orchestration.md#activity-resolve-driver).
  I-12 (no/ambiguous licence match → human review) is handled there.
- **M-9** — confirmed: no multi-DMER-per-PDF and no DMER-split-across-files cases. The one-document,
  one-decision assumption holds; no branch needed for either case.
- Exact schema/version pinning strategy for the GPT-5.1 structuring prompt (a config value, a blob
  in a `prompts/` container, or hardcoded per deployment) is not specified in the architecture
  document — decide before Phase 2 build so `model_version` has something concrete to record.
