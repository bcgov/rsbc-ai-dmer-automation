# Stage 1 — Ingest

Source: architecture doc §4.1. Figure: `docs/architecture/Figure1_Revised_Architecture.png` (top
band, "Stage 1 — Ingest"). Data model: `../data-model.md`. Messages: `../message-contracts.md`.

## Purpose

Move a DMER from Mercury into our control, idempotently, one document at a time. This is
**two separate Azure Functions**, not one — the original architecture performed page retrieval,
download, blob write, DB write and publish in a single invocation, which couples the fate of every
document in a page together: one unreadable pre-signed URL fails the whole batch, and the retry
re-downloads the forty-nine documents that already succeeded. Splitting page retrieval (Page
Poller) from per-document download (Ingest Function) gives per-document retry and dead-lettering.

## Components

| Component | Trigger | Reads | Writes |
|---|---|---|---|
| **Page Poller** | Timer, ~5 min (question I-14 sets the real SLA) | `poll_checkpoint`; Mercury batch GET API, one page per invocation | `dmer_document`, `driver`, `poll_checkpoint` |
| **Ingest Function** | Service Bus queue trigger on `dmer-ingest` | The queue message; the pre-signed URL on the message (or re-read from `dmer_document`) | `raw-dmer` blob container, `dmer_document`, `dmer_stage_run` |
| **Webhook Listener** (optional, real-time path) | HTTP trigger | Mercury's real-time payload | Same as Page Poller (shares the upsert + publish) |

All three are recommended to live in one Azure Functions app (Flex Consumption or Premium plan —
see `../services/azure-functions.md`) since they share `dmer_common` code and none needs container
isolation from the others.

## Page Poller — processing steps

1. Read the `poll_checkpoint` row for this source (`BACKLOG` or `REALTIME`) to obtain `last_page`
   and `last_received_date`.
2. Call the Mercury batch `GET` API for **a single page only**. Do not loop over pages inside the
   invocation — a long-running poller is harder to retry, harder to reason about on timeout, and
   holds the checkpoint open while it works.
3. For each entry in the page's `value` array, **upsert** one `dmer_document` row, keyed on
   `document_guid`: `INSERT ... ON CONFLICT (document_guid) DO NOTHING`. This is the single most
   important idempotency guarantee in the system — it is what makes it safe for the poller to see
   the same document repeatedly, for the webhook to deliver a duplicate, and for a failed run to be
   repeated.
4. If the entry contains a `driver` object, resolve or create the `driver` row from
   `licence_number` (normalized to the canonical 8-digit form with
   `dmer_common.licence.normalize_licence` — see `../data-model.md#driver`) and attach `driver_key` to the
   document. If it does not, leave `driver_key` null. Extraction records the page's licence (`licence_number_read`) but does
   not resolve a driver; [Document Orchestration](03-document-orchestration.md#activity-resolve-driver) does.
5. If the entry contains a `case` object, record `mercury_case_id`.
6. Publish one `dmer-ingest` message per document, with **Service Bus `MessageId` set to
   `document_guid`** so duplicate detection suppresses repeats within the detection window.
7. If `has_more` is true, publish a message instructing the poller to fetch the next page. Each
   page therefore becomes its own short, independently retryable invocation.
8. Update `poll_checkpoint` (`last_page`, `last_received_date`, `last_run_at`).

### Why `DO NOTHING`, not `DO UPDATE`

The batch API is expected to keep returning the same `Uploaded` documents until `dps_date` is set
(question M-3: confirmed — Mercury "claims" a document on batch GET so Intake and the AI pipeline
don't work the same DMER concurrently, but already-in-flight documents can still reappear in a
page). If the poller overwrote the row on every pass, it would reset `pipeline_status` on a
document that is mid-flight through extraction. Mercury-side metadata that genuinely changes
(`dps_date`, `mercury_document_status`, case assignment) is refreshed later, by the driver
orchestration's completeness call — a point where overwriting is safe — not by the poller.

### Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_document` | `INSERT ... ON CONFLICT (document_guid) DO NOTHING` | `id` (generated), `document_guid`, `document_name`, `mercury_document_status`, `document_priority`, `received_date`, `dps_date`, `queue`, `business_area`, `mercury_case_id`, `driver_key` (if supplied), `correlation_id` (generated once), `pipeline_status = RECEIVED`, `current_stage = INGEST`, `attempt_count = 0`, `first_seen_at`, `updated_at`. |
| `driver` | `INSERT ... ON CONFLICT (licence_number) DO UPDATE` | `driver_key`, `licence_number`, `mercury_driver_id`, `first_name`, `last_name`, `last_synced_at`. Only when Mercury returned a driver object. |
| `poll_checkpoint` | `UPDATE` | `source`, `last_page`, `last_received_date`, `last_run_at`. |

### Concurrency

Singleton. Two pollers running at once will fetch the same page twice — harmless (idempotent
upsert) but wasteful. Enforce with a Durable Function singleton pattern, a timer-function
`runOnStartup` lock, or a Postgres advisory lock on the checkpoint row.

### Failure handling

A Mercury call failure is transient: retry inside the function with backoff, let the timer fire
again if the retry budget is exhausted. Nothing is lost because the checkpoint only advances after
a successful page. A failure publishing some but not all messages for a page is also safe — the
next pass re-upserts the same documents and re-publishes; duplicate detection and the idempotent
upsert absorb it.

## Ingest Function — processing steps

1. Load the `dmer_document` row. **If `pipeline_status` is already `DOWNLOADED` or later, complete
   the message and return.** This is the replay guard — a redelivered message must not re-download
   and must not re-publish.
2. Download the document from the pre-signed URL.
3. Write it to the `raw-dmer` container at a **deterministic path**:
   `raw-dmer/{yyyy}/{MM}/{document_guid}.pdf` — so a retry overwrites the same blob rather than
   creating a second copy.
4. Update the document row: `raw_blob_url`, `pipeline_status = DOWNLOADED`.
5. Publish to `dmer-raw`.

### Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_stage_run` | `INSERT` then `UPDATE` | `document_id`, `stage = INGEST`, `status` (`RUNNING` → `SUCCEEDED`/`FAILED`), `attempt_no`, `started_at`, `ended_at`, `output_blob_url` = the raw blob URL, `error_code`/`error_detail` on failure. |
| `dmer_document` | `UPDATE` | `raw_blob_url`, `pipeline_status = DOWNLOADED`, `current_stage = EXTRACT`, `attempt_count` incremented, `updated_at`. |

### Blob writes

One object in `raw-dmer`, the unmodified source PDF — the evidentiary copy. **Never rewritten by a
later stage.** Enable soft delete and versioning on this container (see `../services/azure-blob-storage.md`).

### Concurrency

Scales freely. One document per invocation.

### Failure handling

**Pre-signed URL expiry is a real constraint on the whole design.** If a message reaches the DLQ
and is replayed hours later, it will fail on an expired link, and the replay is worthless unless
a fresh URL can be obtained. Question M-1 (TTL, and whether a fresh URL can be requested per
document later) is **open** — see [Open Questions](#open-questions--decisions-required). Build the
DLQ Drain's "re-poll for a fresh URL" branch from the start (see
[DLQ Drain](10-dlq-drain.md)) rather than discovering the need during the first incident.

## Webhook Listener (optional, real-time path)

HTTP-triggered function. Performs the **same upsert** as the poller and publishes to the **same**
`dmer-ingest` queue, sharing all the same idempotency guarantees. Question M-8 confirms Mercury
does **not currently** emit new-DMER events — so this path has no data source yet. Keep the poller
as the only active intake path until Mercury exposes an event/webhook; scaffold the listener
function but do not wire it to a live Mercury endpoint.

## Interaction with downstream stages

Publishes to `dmer-raw`, consumed by [Extraction](02-extraction.md). No upstream dependency other
than Mercury itself.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `MERCURY_BATCH_API_BASE_URL` | Mercury batch GET endpoint. |
| `MERCURY_AUTH_*` | Mercury credentials, from Key Vault (see `../services/azure-key-vault.md`). Network path is ExpressRoute (question M-4, confirmed). |
| `POLL_INTERVAL_MINUTES` | Timer schedule (default ~5; question I-14 may adjust). |
| `SERVICEBUS_NAMESPACE`, `DMER_INGEST_QUEUE`, `DMER_RAW_QUEUE` | Service Bus wiring — see `../message-contracts.md`. |
| `RAW_DMER_CONTAINER` | Blob container name (default `raw-dmer`). |
| `PRESIGNED_URL_REUSE_POLICY` | Not yet defined — depends on M-1's answer. |

## Authentication / identity

Managed identity for Service Bus (send/receive) and Blob (write to `raw-dmer`) and PostgreSQL.
Mercury credentials come from Key Vault (Mercury is outside our tenant boundary — key/credential
auth, not managed identity, over ExpressRoute).

## Idempotency requirements

- `dmer_document` unique on `document_guid` — the poller/webhook upsert guard.
- Service Bus duplicate detection on `dmer-ingest` (`MessageId = document_guid`) as a second layer.
- Ingest Function's own replay guard (`pipeline_status >= DOWNLOADED` → no-op) as a third layer,
  independent of Service Bus duplicate detection so it also covers a DLQ-Drain-triggered replay
  outside the detection window.

## Logging / auditing

Structured JSON logs via `dmer_common.telemetry` (see `../services/azure-monitor.md`), correlation
ID bound for the life of the document. **Never log the licence number or clinical content** — only
`document_id`/`document_guid`/`driver_key`.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/messaging/publisher.py` and `consumer.py` are reusable as-is
  for `dmer-ingest`/`dmer-raw` (no sessions required on either queue).
- `libs/dmer_common/src/dmer_common/storage/client.py` (`BlobClient`) is reusable for the
  `raw-dmer` write; add a path builder analogous to
  `libs/dmer_common/src/dmer_common/storage/paths.py` for
  `raw-dmer/{yyyy}/{MM}/{document_guid}.pdf`.
- `libs/dmer_common/src/dmer_common/mercury_client/__init__.py` is currently an empty
  anti-corruption-layer stub (docstring only) — the batch GET call, cursor-based pagination
  (question M-2: confirmed cursor-based, `page_size`/`nextLink` shape), and driver/case parsing
  need to be built here.
- `dmer_document`/`poll_checkpoint`/`driver` repositories do not exist yet — see
  `../data-model.md#alignment-gaps-vs-current-code`. Do not reuse `documents.py`'s `DocumentRepository`
  as-is; its status enum doesn't match `pipeline_status`.
- `services/intake-processor/` is the placeholder folder for this stage (currently an empty
  `FunctionApp()` with a `# TODO: register triggers here` comment) — register the Page Poller
  (timer), Ingest Function (Service Bus trigger), and Webhook Listener (HTTP, scaffolded but
  inactive) here.

## Open Questions / Decisions Required

- **M-1** — Pre-signed URL TTL, and whether a fresh URL can be requested for a single document
  later. Blocks whether a DLQ replay can work at all; if not, the DLQ Drain must re-poll Mercury for
  a fresh URL rather than resubmitting the original message.
- **M-6** — Do Mercury's POST/PUT endpoints honour an idempotency key? Affects the Outbox Publisher,
  not Ingest directly, but the same integration-confidence question applies to every Mercury write.
- Poller singleton mechanism (Durable singleton vs. advisory lock vs. accepting the harmless race) —
  not specified in the architecture document; pick one during Phase 1 build.
