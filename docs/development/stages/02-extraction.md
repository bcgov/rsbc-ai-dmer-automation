# Stage 2 — Extraction

Source: architecture doc §4.2. Figure: `docs/architecture/Figure1_Revised_Architecture.png`
("Stage 2 — Extraction, the only container app"). Data model: `../data-model.md`. Messages:
`../message-contracts.md`.

## Purpose

Turn a scanned page into structured data and — just as importantly — establish which driver the
document belongs to. This is **the only Container App in the design**. It earns that because it
runs for tens of seconds to minutes per document, loads models, splits images, and scales on a
completely different curve from the rest of the pipeline (bounded by Document Intelligence/OpenAI
quota, not by cost). See `../services/azure-container-apps.md` for the KEDA scale rule.

## Trigger / reads / writes

| | |
|---|---|
| Type | Azure Container App, Service Bus queue consumer on `dmer-raw`, KEDA scale rule on queue depth |
| Reads | `raw-dmer` blob; Document Intelligence custom model + `prebuilt-ocr`; Azure OpenAI GPT-5.1 |
| Writes | `extracted-dmer` blob, `dmer_extraction`, `driver`, `driver_evaluation`, `dmer_document`, `dmer_stage_run` |
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
7. **Driver resolution.** Read the licence number from the page.
   - If the document already carries a `driver_key` from Mercury, verify the two agree; record a
     discrepancy if they don't.
   - If it doesn't, resolve or create the `driver` row from the licence read, and attach
     `driver_key`.
   - Then create or attach the `driver_evaluation` row for that driver and **increment its expected
     count**.
8. **Combine and hash.** Merge the custom-model result and the handwriting result into one JSON,
   write it to `extracted-dmer`, and compute the canonical comparison hash used later for duplicate
   detection.
9. **Publish.** Send to `dmer-extracted` with `document_id`, `driver_key`, and the extracted blob URL.

### Cut-off detection in practice

On the MV2011C form, the top band carries the fax banner and the heading "DRIVER'S MEDICAL
EXAMINATION"; the bottom band carries the physician signature block, clinic address, and the
MV2011C form footer. Both are printed elements at fixed relative positions, so presence can be
determined from the `prebuilt-ocr` layout output **without an LLM call**. Treat the page as cut off
when **either** band is absent. Run this check before the expensive structuring call if you want to
save quota — but still extract the content, because a cut-off document's fields are needed for
duplicate comparison even when its outcome is superseded (see
[Decision Gateway](07-decision-gateway.md), step 2).

### Why driver resolution moved here

In the original architecture the licence was only used at the decision gateway. Moving it into
extraction has three effects:

- Documents can be grouped by driver from the moment they're extracted, including when Mercury
  returned no driver object.
- The expected document count is established while the batch is still being assembled, so the wait
  is measurable rather than discovered at the end.
- The Mercury `GET by driver_licence` call can be made once early and cached, rather than once per
  document at decision time.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_extraction` | `INSERT` (one row per document) | `document_id`, `licence_number_read`, `exam_date`, `physician_name`, `has_header`, `has_signature`, `is_cutoff`, `page_count`, `confidence_avg`, `comparison_fields` (jsonb, canonicalized subset), `comparison_hash` (sha256 of the canonicalized subset). |
| `driver` | `INSERT ... ON CONFLICT (licence_number) DO UPDATE` | Only when the licence was read from the page and no driver row existed. |
| `driver_evaluation` | `INSERT ... ON CONFLICT (driver_key, open) DO UPDATE` | `id`, `driver_key`, `status = WAITING`, `expected_document_count` (from Mercury `GET by driver_licence`), `completed_document_count` (unchanged here), `last_mercury_check_at`. See `../data-model.md#open-questions--decisions-required` re: the `(driver_key, open)` conflict target. |
| `dmer_document` | `UPDATE` | `driver_key` (if resolved here), `pipeline_status = EXTRACTED`, `current_stage = NORMALIZE`, `updated_at`. |
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = EXTRACT`, `status`, `attempt_no`, `started_at`, `ended_at`, `output_blob_url` = the combined extraction blob, `model_version` (custom DI model version **and** the GPT prompt/schema version — record both), error fields on failure. |

## Blob writes

One object in `extracted-dmer`, holding the **combined** JSON. Write the custom-model result and
the handwriting result as **named sections within that one object**, not as separate blobs —
keeping them together means a replay of normalization has everything it needs from a single fetch.

> Do not replicate the older per-file layout (`top_level.json` / `ocr.json` / `handwritten.json` in
> one container, `combined.json` in a second `combined-extracted-dmer` container) that
> `libs/dmer_common/src/dmer_common/storage/paths.py` currently implements — see
> [Alignment gaps](#alignment-gaps-vs-current-code).

## Failure handling

- HTTP 429 from Document Intelligence or Azure OpenAI is **transient**: retry inside the handler
  with exponential backoff, never by abandoning the message — abandoning consumes delivery count
  and will dead-letter a perfectly good document during a busy period.
- An unreadable or malformed PDF is **poison**: dead-letter it explicitly with a reason rather than
  retrying five times.
- **Renew the Service Bus lock while extraction runs**, or a slow document will be redelivered
  while it is still being processed (lock duration on `dmer-raw` is 5 minutes; extraction can take
  minutes).

## Interaction with upstream/downstream stages

Consumes `dmer-raw` (from [Ingest](01-ingest.md)). Publishes `dmer-extracted`, consumed by
[Document Orchestration](03-document-orchestration.md). Also the stage that creates/updates
`driver_evaluation`, which [Driver Orchestration](06-driver-orchestration.md) later reads.

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
`EXTRACTED` document (e.g. a redelivered message after a lock-renewal failure) must not double the
`driver_evaluation.expected_document_count` increment or create a duplicate `dmer_extraction` row —
use `document_id` as the natural upsert key on `dmer_extraction`.

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
  "blob_url": "https://.../extracted-dmer/8f3c1b2a.json",
  "attempt": 1,
  "enqueued_at": "2026-09-18T12:05:00Z"
}
```

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/doc_intelligence/client.py` (`DocumentIntelligenceClient`,
  `analyze()`) and `openai_client/client.py` (`OpenAIClient.complete()`) are reusable as-is — both
  already wrap retry + circuit breaker (`retry/policies.py`: `di_retry`/`di_breaker`,
  `openai_retry`/`openai_breaker`) matching the failure-handling rules above.
- `libs/dmer_common/src/dmer_common/storage/paths.py` and `containers.py` need to be **rewritten**,
  not extended — they implement the old `extracted-dmer` + `combined-extracted-dmer` two-container,
  per-substep layout. Replace with a single `extracted-dmer/{document_id}.json` path holding named
  sections (`top_level`, `ocr`, `handwritten`, `combined`) in one object.
- `services/di-processor/` is the placeholder folder for this stage (currently
  `main() -> raise NotImplementedError`) — this is where the Container App entrypoint, KEDA scaling
  config, and the nine processing steps above belong.
- No `dmer_extraction`/`driver`/`driver_evaluation` repository exists yet — build alongside the
  `dmer_document` repository rework noted in `../data-model.md#alignment-gaps-vs-current-code`.

## Open Questions / Decisions Required

- **I-12** — escalation when the licence read matches no driver, or matches more than one: send for
  human review, mentioning the licence info and reason in the comment (answered) — confirm the exact
  `pipeline_status`/outcome this maps to (likely `MANUAL_REVIEW` with a `dmer_decision` fallback,
  same shape as [Post-Processing](08-post-processing.md)'s fallback path).
- **M-9** — confirmed: no multi-DMER-per-PDF and no DMER-split-across-files cases. The one-document,
  one-decision assumption holds; no branch needed for either case.
- Exact schema/version pinning strategy for the GPT-5.1 structuring prompt (a config value, a blob
  in a `prompts/` container, or hardcoded per deployment) is not specified in the architecture
  document — decide before Phase 2 build so `model_version` has something concrete to record.
