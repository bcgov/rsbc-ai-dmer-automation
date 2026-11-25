# Stage 1 — Ingest

Source: architecture doc §4.1. Figure: `docs/architecture/Figure1_Revised_Architecture.png` (top
band, "Stage 1 — Ingest"). Data model: `../data-model.md`. Messages: `../message-contracts.md`.

## Purpose

Move a DMER from Mercury into our control, idempotently, one document at a time. This is
**two separate Azure Functions**, not one — the original architecture performed page retrieval,
download, blob write, DB write and publish in a single invocation, which couples the fate of every
document in a page together: one failed download fails the whole batch, and the retry
re-downloads the forty-nine documents that already succeeded. Splitting page retrieval (Page
Poller) from per-document download (Ingest Function) gives per-document retry and dead-lettering.

## Components

| Component | Trigger | Reads | Writes |
|---|---|---|---|
| **Page Poller** | Timer, ~5 min (question I-14 sets the real SLA) | Mercury `GET dmers`, one page per invocation | `dmer_document`, `driver`, `poll_checkpoint`; Mercury's AI Processed status |
| **Ingest Function** | Service Bus queue trigger on `dmer-ingest` | The queue message (for `document_id`); the `dmer_document` row (its `document_guid`); Mercury `GET dmers/{documentRecordId}/files` for the PDF | `raw-dmer` blob container, `dmer_document`, `dmer_stage_run` |
| **Webhook Listener** (optional, real-time path) | HTTP trigger | Mercury's real-time payload | Same as Page Poller (shares the upsert + publish) |

All three are recommended to live in one Azure Functions app (Flex Consumption or Premium plan —
see `../services/azure-functions.md`) since they share `dmer_common` code and none needs container
isolation from the others.

## Page Poller — processing steps

1. Call Mercury's `GET {MERCURY_API_BASE_URL}?count={MERCURY_PAGE_SIZE}` (`GET dmers`; the page
   size is Mercury's `count` query parameter — `pageSize` is ignored, Mercury DEV's Swagger,
   2026-11-24). It is **claim-based**: it returns the next records not yet marked AI Processed,
   so there is no cursor. Mercury returns **no `nextLink`** (confirmed 2026-11-24); a page that
   comes back **full** (as many items as asked for) means more may be waiting. The response's
   `count` echoes the page size asked for; it is only logged. By default **one page per run**;
   `MERCURY_MAX_PAGES_PER_POLL` (default 1) lets one run read further full pages, each stored,
   queued and claimed before the next is read (see step 8).
   Do not loop over pages inside the invocation — a long-running poller is harder to retry, harder
   to reason about on timeout, and holds the advisory lock while it works.
   `MercuryClient` translates Mercury's camelCase `items` into the snake_case records below
   (`documentRecordId` → `document_guid`, `dmerDocumentCaseRecord` → `case`, …). `receivedDate`
   ("MM/DD/YYYY HH:MM:SS", no zone) is read in `MERCURY_RECEIVED_DATE_TIMEZONE` (default
   `America/Vancouver`) until Mercury confirms its zone; an ISO-8601 value keeps its own offset.
2. Keep only the **DMERs** (`documentType` = `DMER`). The page also holds other document types
   (HRDIR reports, backfiles — no `documentUrl`); they are not stored, but are still marked in
   step 7.
3. For each DMER, **upsert** one `dmer_document` row, keyed on
   `document_guid`: `INSERT ... ON CONFLICT (document_guid) DO NOTHING`. This is the single most
   important idempotency guarantee in the system — it is what makes it safe for the poller to see
   the same document repeatedly, for the webhook to deliver a duplicate, and for a failed run to be
   repeated.
4. If the entry contains a `driver` object, resolve or create the `driver` row from
   `licence_number` (normalized to the canonical 8-digit form with
   `dmer_common.licence.normalize_licence` — see `../data-model.md#driver`) and attach `driver_key` to the
   document. If it does not, leave `driver_key` null. Extraction records the page's licence (`licence_number_read`) but does
   not resolve a driver; [Document Orchestration](03-document-orchestration.md#activity-driver-lookup) does.
5. If the entry contains a `case` object, record `mercury_case_id`.
6. Publish one `dmer-ingest` message per DMER, with **Service Bus `MessageId` set to
   `document_guid`** so duplicate detection suppresses repeats within the detection window.
7. **Mark every record on the page AI Processed** — DMERs and non-DMERs alike:
   `POST {MERCURY_API_BASE_URL}/BatchSetDmerAIProccessedStatus` with `{"dmerIds": [<document_guid>, …]}`
   (Mercury's endpoint name, spelling included). Mercury sets their `aiProcessedStatus` to
   **InProgress** (confirmed 2026-11-20 — the pipeline never sets InProgress itself), moves them to
   the AI Processed Queue, out of the DPS queues, and `GET dmers` stops returning them — this is
   what brings the next page. The outcome call (`SetDmerAIProccessedStatus`, see
   [Post-Processing](08-post-processing.md)) later sets Proccessed or Error. **Non-DMER records**
   get one too: before this claim, each is stored as `MANUAL_REVIEW` with an error outcome queued
   ("not a DMER", Team - Intake), due 5 minutes later so it lands after the claim
   (`dmer_common.db.non_dmer`, `NON_DMER_OUTCOME_DELAY_SECONDS`, decided 2026-11-23). A DMER that
   stalls without an outcome would stay InProgress in Mercury.
   It comes **last**, once the DMERs are stored and queued: if it fails, the poll fails and the next
   tick gets the same records again, which steps 3–6 handle idempotently. Marking first could hide
   a DMER from the DPS queues that the pipeline never stored.
8. **One page per timer tick by default, no self-continuation.** (Opt-in since 2026-11-24:
   `MERCURY_MAX_PAGES_PER_POLL` > 1 reads further pages in the same run while each comes back
   full — for draining the backlog faster. The cap bounds the run's length and how much it pushes
   into extraction; each page is claimed before the next is read, so a failure mid-run loses
   nothing.) The architecture document describes the
   poller publishing a message to itself to fetch the next page immediately when `nextLink` is
   present, but no such queue exists in `../message-contracts.md`'s four documented queues, and
   adding a fifth control queue for it was judged not worth the extra infrastructure for now.
   Deliberately chosen over looping through pages inside one invocation too (which would have kept
   the design queue-free but broken step 1's "single page only" rule for a different reason). A
   large backlog therefore drains at one page per tick — revisit if backlog-drain speed
   (~1,000,000 documents, question I-15) turns out to need it faster.
9. Update `poll_checkpoint` (`last_received_date`, `last_run_at`; `last_cursor` stays null — there
   is no cursor).

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
| `dmer_document` | `INSERT ... ON CONFLICT (document_guid) DO NOTHING` | `id` (generated — this row's own id doubles as the tracing key on every log line/message; no separate `correlation_id`), `document_guid`, `document_name`, `mercury_document_status`, `document_priority`, `received_date`, `dps_date`, `queue`, `business_area`, `mercury_case_id`, `driver_key` (if supplied), `document_url` (Mercury's `documentUrl`, a path inside Mercury — kept for reference, not used to download), `pipeline_status = RECEIVED`, `current_stage = INGEST`, `attempt_count = 0`, `first_seen_at`, `updated_at`. |
| `driver` | `INSERT ... ON CONFLICT (licence_number) DO UPDATE` | `driver_key`, `licence_number`, `mercury_driver_id`, `first_name`, `last_name`, `last_synced_at`. Only when Mercury returned a driver object. |
| `poll_checkpoint` | `UPDATE` | `source`, `last_cursor` (null), `last_received_date`, `last_run_at`. |

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

1. Load the `dmer_document` row (by `document_id` from the message). **If `pipeline_status` is
   already `DOWNLOADED` or later, complete the message and return.** This is the replay guard — a
   redelivered message must not re-download and must not re-publish.
2. Download the PDF from Mercury by the row's `document_guid`:
   `GET {MERCURY_API_BASE_URL}/{documentRecordId}/files` (bearer key, as `GET dmers`). Mercury's
   `documentUrl` is a path inside Mercury (`dfp_driver/<driverId>/…`), not a link we can fetch. The
   response must be the PDF itself (it must start with `%PDF-`); anything else fails the download.
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

The download is retried with backoff inside the invocation (behind the Mercury circuit breaker);
if it still fails, the message is redelivered and finally dead-lettered. Because the file is
fetched by `documentRecordId` on demand — not from a pre-signed URL — there is nothing to expire:
a DLQ replay hours later downloads the same document again (question M-1, answered).

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
| `MERCURY_API_BASE_URL` | Mercury `GET dmers` endpoint (`https://<host>/Dmers`); the AI Processed call is `<it>/BatchSetDmerAIProccessedStatus` and the file download `<it>/{documentRecordId}/files`. |
| `MERCURY_PAGE_SIZE` | Records per `GET dmers` call (default 50). |
| `MERCURY_RECEIVED_DATE_TIMEZONE` | IANA zone `receivedDate` is read in when it has no offset (default `America/Vancouver`). |
| `MERCURY_AUTH_TOKEN_URL` | Mercury's token endpoint (`https://<host>/Authentication/token`). Every Mercury call carries `Authorization: Bearer <token>`; the token comes from `GET <this>?secret=<MERCURY_API_KEY>`, is shared by the process, reused until it expires (`expiresIn`/`expiresAt` in the response, else the JWT `exp`, else `MERCURY_TOKEN_TTL_SECONDS`, default 1800) and fetched again early, or at once when an API answers 401. |
| `MERCURY_API_KEY` | The secret exchanged for the token, from Key Vault (see `../services/azure-key-vault.md`). Never logged, nor the token URL that carries it. Network path is ExpressRoute (question M-4, confirmed). |
| `POLL_INTERVAL_MINUTES` | Timer schedule (default ~5; question I-14 may adjust). |
| `SERVICEBUS_NAMESPACE`, `DMER_INGEST_QUEUE`, `DMER_RAW_QUEUE` | Service Bus wiring — see `../message-contracts.md`. |
| `RAW_DMER_CONTAINER` | Blob container name (default `raw-dmer`). |

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

Structured JSON logs via `dmer_common.telemetry` (see `../services/azure-monitor.md`), `document_id`
bound for the life of the document. **Never log the licence number or clinical content** — only
`document_id`/`document_guid`/`driver_key`.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/messaging/publisher.py` and `consumer.py` are reusable as-is
  for `dmer-ingest`/`dmer-raw` (no sessions required on either queue).
- `libs/dmer_common/src/dmer_common/storage/client.py` (`BlobClient`) is reusable for the
  `raw-dmer` write; add a path builder analogous to
  `libs/dmer_common/src/dmer_common/storage/paths.py` for
  `raw-dmer/{yyyy}/{MM}/{document_guid}.pdf`.
- `libs/dmer_common/src/dmer_common/mercury_client/__init__.py` is the anti-corruption layer:
  `get_page` (`GET dmers`, items translated to snake_case records) and `set_ai_processed` (the
  claim). Mercury's shapes stay inside it.
- `dmer_document`/`poll_checkpoint`/`driver` repositories do not exist yet — see
  `../data-model.md#alignment-gaps-vs-current-code`. Do not reuse `documents.py`'s `DocumentRepository`
  as-is; its status enum doesn't match `pipeline_status`.
- `services/intake-processor/` is the placeholder folder for this stage (currently an empty
  `FunctionApp()` with a `# TODO: register triggers here` comment) — register the Page Poller
  (timer), Ingest Function (Service Bus trigger), and Webhook Listener (HTTP, scaffolded but
  inactive) here.

## Open Questions / Decisions Required

- **M-1** — *Answered:* there is no pre-signed URL; the PDF is downloaded on demand with
  `GET dmers/{documentRecordId}/files`, so a DLQ replay simply downloads again. Still to confirm with
  Mercury: the response is the raw PDF (`application/pdf`), and 404 means an unknown document.
- **M-6** — Do Mercury's POST/PUT endpoints honour an idempotency key? Affects the Outbox Publisher,
  not Ingest directly, but the same integration-confidence question applies to every Mercury write.
- Poller singleton mechanism (Durable singleton vs. advisory lock vs. accepting the harmless race) —
  not specified in the architecture document; pick one during Phase 1 build.
