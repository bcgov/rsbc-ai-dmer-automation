# DMER Pipeline — End-to-End Walkthrough

**Who this is for:** an engineer new to the project who wants to understand, in one read, what
happens to a DMER from the moment it lands in Mercury until its outcome is back in Mercury — every
stage, every database write, every Mercury API call, and every scenario (happy path, duplicates,
missing drivers, failures).

**How it was written:** from the code on `feat/mercury-api-integration` (November 2026), checked
file by file. Where the older stage docs (`stages/*.md`) disagree with the code, this document
follows the **code** and lists the differences in [§12](#12-where-the-stage-docs-and-the-code-disagree).
Use the stage docs for design rationale and history; use this one for "what actually happens".

Contents

1. [The one requirement](#1-the-one-requirement)
2. [The big picture](#2-the-big-picture)
3. [Identifiers you will see everywhere](#3-identifiers-you-will-see-everywhere)
4. [Status models](#4-status-models)
5. [Mercury API reference](#5-mercury-api-reference)
6. [Stage 1 — Ingest](#6-stage-1--ingest-intake-processor)
7. [Stage 2 — Extraction](#7-stage-2--extraction-di-processor)
8. [Stage 3 — Document orchestration](#8-stage-3--document-orchestration-workflow-orchestrator)
9. [Stage 4 — Driver orchestration and the decision](#9-stage-4--driver-orchestration-and-the-decision-driver-orchestrator)
10. [Stage 5 — Delivering the outcome to Mercury, and the safety nets](#10-stage-5--delivering-the-outcome-and-the-safety-nets-reliability)
11. [Scenario catalogue](#11-scenario-catalogue)
12. [Where the stage docs and the code disagree](#12-where-the-stage-docs-and-the-code-disagree)
13. [Known gaps and open questions](#13-known-gaps-and-open-questions)
14. [Configuration cheat sheet](#14-configuration-cheat-sheet)
15. [Where to look in the code](#15-where-to-look-in-the-code)

---

## 1. The one requirement

> Once this solution is in production, Intake will look only at the AI-processed queue. A DMER
> whose outcome is never written back to Mercury is invisible to both the AI queue and the manual
> queue. It is not delayed; it is lost.

Every piece of machinery that looks like over-engineering — the outbox table, explicit waiting
states, the sweeper, the DLQ drain, the fallback outcomes — exists so that **every DMER the pipeline
claims from Mercury ends with an outcome written back to Mercury**, even when the AI can't decide.

---

## 2. The big picture

### 2.1 Flow

```
 Mercury (Dynamics 365, on OpenShift)
   │  GET Dmers (claim-based page)          ▲  SetDmerAIProccessedStatus / add_driver / add_case
   ▼                                        │
 ┌──────────────────────────────┐           │
 │ STAGE 1  Ingest              │  intake-processor (Azure Functions)
 │  Page Poller (timer, 5 min)  │──► dmer-ingest ──► Ingest Function: download PDF → raw-dmer blob
 └──────────────────────────────┘                        │
                                                         ▼ dmer-raw
 ┌──────────────────────────────┐
 │ STAGE 2  Extraction          │  di-processor (Container App, KEDA-scaled)
 │  Doc Intelligence + GPT      │──► extracted-dmer/<doc>/{top_level,ocr,handwritten,combined}.json
 └──────────────────────────────┘                        │
                                                         ▼ dmer-extracted
 ┌──────────────────────────────┐
 │ STAGE 3  Document            │  workflow-orchestrator (Durable Functions, one instance per DMER)
 │  orchestration               │  DriverLookup → NormalizeDmer → RunRuleEngine → SignalDriver
 └──────────────────────────────┘                        │
                                                         ▼ driver-decision (session = driver_key)
 ┌──────────────────────────────┐
 │ STAGE 4  Driver orchestration│  driver-orchestrator (session-triggered Function, one driver at a time)
 │  Decision Gateway            │  completeness → decide → dmer_decision + mercury_outbox (one txn)
 └──────────────────────────────┘                        │
                                                         ▼ mercury_outbox table
 ┌──────────────────────────────┐
 │ STAGE 5  Reliability         │  reliability (Azure Functions, timers)
 │  Outbox Publisher (1 min)    │──► Mercury writes (above)
 │  Reconciliation Sweeper (15m)│    re-publishes stalled work
 │  DLQ Drain (5 min)           │    classifies dead letters, redrives or falls back
 │  Daily reconciliation (1/day)│    report of anything unaccounted for
 └──────────────────────────────┘
```

Stages 1–3 are **per document** and run in parallel. Stage 4 is where a driver's DMERs are
**joined** — one driver at a time, never two decisions for the same driver concurrently. Stage 5
is not processing: it's what makes sure nothing is lost.

### 2.2 Components and where they run

| Service (repo folder) | Azure compute | What it does |
|---|---|---|
| `services/intake-processor` | Function App | Page Poller (timer), Ingest Function (`dmer-ingest`), webhook scaffold (returns 501) |
| `services/di-processor` | Container App (the only one) | Extraction: Document Intelligence custom model + OCR + Azure OpenAI handwriting, merge |
| `services/workflow-orchestrator` | Function App (Durable) | Per-document orchestration and its activities |
| `services/driver-orchestrator` | Function App | Per-driver decision on the `driver-decision` session queue |
| `services/reliability` | Function App | Outbox Publisher, Reconciliation Sweeper, DLQ Drain, Daily reconciliation |
| `libs/dmer_common` | (library) | DB repositories, Mercury client, messaging, storage, normalization, rules, retry |
| `services/intake-processor/tests/local/mock-mercury-function-app` | Function App (DEV only) | Mock of every Mercury API used in DEV runs |

### 2.3 Queues (Azure Service Bus)

| Queue | Producer → consumer | Notes |
|---|---|---|
| `dmer-ingest` | Page Poller → Ingest Function | Duplicate detection on (5 min window), `MessageId = document_guid` |
| `dmer-raw` | Ingest Function → di-processor | Lock 5 min (**no lock renewal yet**), max delivery 5 |
| `dmer-extracted` | di-processor → workflow-orchestrator | `MessageId = uuid5("dmer-extracted:<document_id>")` |
| `driver-decision` | workflow-orchestrator (and sweeper, re-checks) → driver-orchestrator | **Sessions**, `SessionId = driver_key` |

Every queue has a dead-letter sub-queue (`<queue>/$DeadLetterQueue`) drained by the DLQ Drain.
All message bodies are **camelCase** and carry **pointers only** (ids, blob URLs) — never a licence
number or clinical text:

```json
{ "messageId": "…", "schemaVersion": "1.0", "documentId": "<dmer_document.id>",
  "documentGuid": "<Mercury documentRecordId>", "driverKey": "<driver.driver_key or null>",
  "blobUrl": "<artifact the next stage needs>", "attempt": 1, "enqueuedAt": "<UTC ISO>" }
```

### 2.4 Blob storage

| Container | Written by | Path | Content |
|---|---|---|---|
| `raw-dmer` | Ingest | `<yyyy>/<MM>/<document_guid>.pdf` | The PDF exactly as Mercury served it |
| `extracted-dmer` | Extraction | `<document_id>/top_level.json` | Custom Document Intelligence model fields `{value, confidence}` + cut-off evidence |
| | | `<document_id>/ocr.json` | Tiled `prebuilt-read` OCR of the page |
| | | `<document_id>/handwritten.json` | GPT handwriting reconstruction `{value, confidence high/low, source}` |
| | | `<document_id>/combined.json` | **The merged result — the only extraction file downstream reads** |
| `normalized-dmer` | Normalize | `<document_id>/runs/<run_id>/normalized.json` | Normalized, typed DMER the rules run on |
| `rules` | (deployed by hand) | `active/rules.json` | The active GoRules ZEN ruleset |
| `reconciliation-reports` | Daily reconciliation | `<YYYY-MM-DD>.json` | The daily report |

### 2.5 Database tables (PostgreSQL) — the ones that matter

| Table | One row per | Key columns |
|---|---|---|
| `dmer_document` | DMER we received | `id` (= `document_id`), `document_guid` (Mercury id, unique), `pipeline_status`, `current_stage`, `driver_key`, `driver_resolved_by`, `mercury_case_id`, `received_date`, `raw_blob_url`, `attempt_count` |
| `driver` | driver | `driver_key`, `licence_number` (8 digits, unique, **PII**), `mercury_driver_id`, `first_name`, `last_name` |
| `dmer_extraction` | document | `licence_number_read`, `is_cutoff`, `has_header`, `has_signature`, `comparison_fields`, `comparison_hash` |
| `rule_evaluation` | rule run | `selected_outcome_code`, `selected_reason`, `priority_rank`, `rules_version`, `all_outcomes`, `fit_letter` |
| `driver_evaluation` | driver **batch** | `status`, `open`, `expected_document_count`, `completed_document_count`, `last_mercury_check_at`, `decision_summary` |
| `driver_evaluation_document` | document in a batch | `(driver_evaluation_id, document_id)` — insert-once marker |
| `dmer_decision` | decided document | `outcome_code`, `is_duplicate`, `duplicate_of_document_id`, `proposed_driver_key`, `decision_reason`, `decided_by` (AI / FALLBACK) |
| `mercury_outbox` | intended Mercury write | `operation`, `payload` (jsonb), `idempotency_key` (unique), `status` (PENDING/SENT/FAILED), `attempt_count`, `next_attempt_at`, `last_error`, `mercury_response` |
| `dmer_stage_run` | attempt of a stage | `stage`, `status` (RUNNING/SUCCEEDED/FAILED), `attempt_no`, `error_code`, `output_blob_url`, `model_version` |
| `processing_error` | failure / sweep / drain event | `stage`, `failure_category`, `reason_code`, `resolution`, `dlq_message_id`, `redrive_count` |
| `message_idempotency` | consumed message (di-processor) | `(scope, message_id)`, claim status + lease |
| `poll_checkpoint` | poll source | `last_run_at` |
| `rules_version` | registered ruleset | `version` (`YYYY-MM-DD[.N]`), `checksum` (sha256 of `rules.json`) |

---

## 3. Identifiers you will see everywhere

| Name | Where it comes from | Use it for |
|---|---|---|
| `document_guid` | Mercury's `documentRecordId` | Every Mercury call about that DMER (`dmerId`, URL paths). **Not** a business key inside the pipeline |
| `document_id` | `dmer_document.id`, generated at Ingest | Every DB join, log line, blob path, message. The tracing key |
| `driver_key` | `driver.driver_key` | The batch key; the `driver-decision` SessionId. Stands in for the licence on queues and logs |
| `licence_number` | Mercury's driver, or read off the page and normalized to 8 digits | Mercury driver lookup, `add_driver`, `add_case`. **PII — database only, never logged or queued** |
| `mercury_driver_id` | Mercury's `driverId` (GUID) | Stored on `driver`; carried on outbox payloads |
| `mercury_case_id` | Mercury's `dmerDocumentCaseRecord.caseId` at Ingest | Decides whether the DMER already has a case |
| `driver_evaluation.id` | Batch id | One per batch of a driver's DMERs decided together |

---

## 4. Status models

### 4.1 `dmer_document.pipeline_status` (one document)

```
RECEIVED ─► DOWNLOADED ─► EXTRACTING ─► EXTRACTED ─► NORMALIZED ─► RULES_APPLIED
 (Ingest:    (Ingest)      (Extraction)   (Extraction;   (Normalize)   (Rule Engine)
  poller)                                  Driver Lookup                    │
                                           runs here)                      ▼
                                                              AWAITING_DRIVER_COMPLETION (SignalDriver)
                                                                           │
                                                                           ▼  (Decision Gateway)
                                                                        POSTING
                                                                           │  (Outbox Publisher)
                                                          ┌────────────────┴───────────────┐
                                                          ▼                                ▼
                                                      COMPLETED                     MANUAL_REVIEW
                                                 (every write SENT)      (failed driver link, outcome sent)

MANUAL_REVIEW is reachable from any non-terminal status (failures, unreadable licence, …).
COMPLETED and MANUAL_REVIEW are terminal (the DLQ Drain may reopen MANUAL_REVIEW for a redrive).
```

`current_stage` (INGEST, EXTRACT, NORMALIZE, RULES, DECISION, POST, plus DRIVER_LOOKUP in stage
runs) tells you **where** the document is, or where it stopped when it went to MANUAL_REVIEW.

### 4.2 `driver_evaluation.status` (one driver batch)

```
WAITING ─► READY ─► EVALUATING ─► DECIDED ─► POSTED
   ▲  └─► STALE ─┘       │            ▲
   └─────────────────────┘ (failure)  └── route_batch_to_manual_review (WAITING/STALE → DECIDED)
```

- **WAITING** — not all of the driver's DMERs are in yet (or Mercury couldn't be checked).
- **STALE** — set only by the Sweeper when a WAITING batch stopped moving; it re-signals the driver.
- **DECIDED** — outcomes and outbox rows written; batch closed (`open = false`). The driver's next
  DMER starts a new batch (I-10).
- **POSTED** — set by the Outbox Publisher when every document of the batch is finished.

### 4.3 `mercury_outbox.status` (one Mercury write)

`PENDING` → `SENT`, or `PENDING` → (retry with backoff) → … → `FAILED` (rejected, or 10 attempts).
`FAILED` alerts once and stays — a person resolves it in Mercury.

### 4.4 Mercury's `aiProcessedStatus` (on the DMER, in Mercury)

| When | Set by | Value |
|---|---|---|
| Before the pipeline sees it | — | NA / empty |
| We claim the page (`BatchSetDmerAIProccessedStatus`) | Mercury | **InProgress** |
| We post the outcome (`SetDmerAIProccessedStatus`) | us | **Proccessed**, or **Error** |

Separately, Mercury's `document_status` (`submittalStatus`) stays **`Uploaded`** until a person
reviews the DMER — our claim and our outcome don't change it.

---

## 5. Mercury API reference

Mercury is outside our tenant, reached over ExpressRoute. All calls go through
`libs/dmer_common/src/dmer_common/mercury_client/`. **Every call carries
`Authorization: Bearer <token>`.**

### 5.1 Authentication — `GET {MERCURY_AUTH_TOKEN_URL}?secret={MERCURY_API_KEY}`

- Returns the bearer token (JSON `token`/`accessToken`/…, or plain text).
- Expiry: `expiresIn` / `expiresAt` from the response, else the JWT `exp`, else
  `MERCURY_TOKEN_TTL_SECONDS` (1800). Refreshed 60 s before expiry (or half-way for short tokens).
- **One token per process**, shared by every client, fetched under a lock (`auth.py`).
- A **401** from any API drops the token, fetches a new one and retries the call **once**.
- The secret is in the URL, so neither the URL nor the token is ever logged.

### 5.2 Reads

| # | Call | Used by | Request | Response (what we use) |
|---|---|---|---|---|
| R1 | `GET {MERCURY_API_BASE_URL}?pageSize=50` | Page Poller, Daily reconciliation | — | `{items:[…], count, nextLink}`. **Claim-based**: returns the next records not yet claimed; `nextLink` is never followed |
| R2 | `POST {MERCURY_API_BASE_URL}/BatchSetDmerAIProccessedStatus` | Page Poller | `{"dmerIds":[<documentRecordId>…]}` | Claims the records: `aiProcessedStatus = InProgress`, moved to the **AI Processed Queue**, `GET Dmers` stops returning them |
| R3 | `GET {MERCURY_API_BASE_URL}/{documentRecordId}/files` | Ingest Function | — | The PDF bytes (checked to start with `%PDF-`) |
| R4 | `POST {MERCURY_DRIVER_LICENCE_API_BASE_URL}/lookup` | Driver Lookup, Decision Gateway, case check | `{"driverLicenceNumber":"<8 digits>"}` | One driver (snake_case): `driver_id`, names, `licence_number`, `documents[]` (`file_name`, `document_type`, `document_status`, `uploaded_date`, `document_id`), driver-level `case`. 404 / empty = no driver |

`GET Dmers` item fields used: `documentRecordId`, `documentName`, `documentType`,
`submittalStatus`, `documentPriority`, `receivedDate`, `queue`, `documentTypeBusinessArea`,
`documentUrl` (a path inside Mercury, stored for reference only), `driver{driverId, firstName,
lastName, licenceNumber}`, `dmerDocumentCaseRecord{caseId, …}`. The batch and the lookup return
only **active** cases.

Reads are retried in-call (4 attempts, exponential 1–20 s) behind a circuit breaker
(5 failures → open 30 s).

### 5.3 Writes (sent only by the Outbox Publisher)

Base: `MERCURY_WRITE_API_BASE_URL` (the Mercury host). Every write sends the outbox row's
`Idempotency-Key` header. No in-call retry — the publisher retries per row with backoff.

**W1 — Outcome: `POST dmers/SetDmerAIProccessedStatus`** (`ai_status.py`)

```json
{ "dmerId": "<documentRecordId>", "aiProcessedStatus": 100000002,
  "aiDpsPriority": 100000000, "aiQueue": 100000000, "aiDmerStatus": 100000003,
  "aiIsDuplicate": true, "notes": "<comment>" }
```

| Case | aiProcessedStatus | aiDpsPriority | aiQueue | aiDmerStatus | notes |
|---|---|---|---|---|---|
| CP | Proccessed | Regular | Team - Intake | Manual Pass | AI comment |
| IN | Proccessed | Regular | Team - Intake | Adjudicate | AI comment |
| PR | Proccessed | Regular | Team - Adjudicators | Adjudicate | AI comment |
| PU | Proccessed | Urgent/ Immediate | Team - Adjudicators | Adjudicate | AI comment |
| TCM | Proccessed | Regular | Team - Case Managers | Adjudicate | AI comment |
| CR | Proccessed | Critical Review | Team - Adjudicators | Adjudicate | AI comment |
| Duplicate | Proccessed | — | — | — | `aiIsDuplicate: true`, "Duplicate of DMER ‹guid›." |
| Processing error (any fallback) | **Error** | Regular | Team - Intake | Adjudicate | "Unable to AI process due to error. ‹details›" |
| Driver link failed | **Error** | Regular | Team - Intake | Adjudicate | "Unable to AI process due to error: the driver could not be linked to the DMER (‹error›). ‹AI comment›" |
| Case creation failed | (normal for the outcome) | | | | AI comment + "The case could not be created for this DMER (‹error›)." |

Option-set values: `aiProcessedStatus` NA 100000000, InProgress 100000001, Proccessed 100000002,
Error 100000003 · `aiDmerStatus` Adjudicate 100000000, Reject 100000001, Clean Pass 100000002,
Manual Pass 100000003 · `aiQueue` Team - Intake 100000000, Team - Adjudicators 100000001,
Team - Case Managers 100000002, Team - DIP Team Lead 100000003, Team - Remedial Intake 100000004,
Team - DIP Adjudicators 100000005 · `aiDpsPriority` Regular 100000000, Urgent/ Immediate
100000001, Expedited 100000002, Critical Review 100000003.

**W2 — Link a driver: `POST dmers/{documentRecordId}/add_driver`** (`driver_link.py`)

```json
{ "driverLicenceNumber": "<licence>", "firstName": "<Mercury first name>", "surname": "<Mercury last name>",
  "comments": "Driver linked by the AI from the licence number read on the DMER.",
  "dmerId": "<documentRecordId>" }
```
Response `{"resultMessage", "driverOpenCase": {"caseId", "caseTitle", "caseType", "dmerStatus"}}`.

**W3 — Give the DMER a case: `POST dmers/{documentRecordId}/add_case`** (`case_link.py`)

```json
{ "case_type": "DMER", "owner": "TBD", "driver": { "driver_id": "<licence number>" },
  "document_type": "DMER",
  "comments": "Case created by the AI: the DMER had no case and the driver had no open case." }
```
Response `{"caseId", "documentRecordId", "status": "Created"}`; 404 driver not found; 409
different driver or DMER already has a case.

**Errors on any write.** Retryable: network errors, timeouts, 408, 429, 5xx, open breaker.
Rejected (no retry): any other non-2xx, **and** a body `{"errorStatus": 1, "replyMessage": …}`
even with HTTP 200 (the reply message is kept on the row's `last_error`).

---

## 6. Stage 1 — Ingest (`intake-processor`)

### 6.1 Page Poller — timer `DMER_POLL_SCHEDULE` (every 5 min)

1. **Singleton:** `pg_try_advisory_lock(727100001)` on a dedicated connection; if another instance
   holds it, this tick does nothing.
2. **Read a page** — R1 `GET Dmers?pageSize=50`. Items are translated to snake_case records
   (`documentRecordId` → `document_guid`, `submittalStatus` → `document_status`, …).
3. **Keep DMERs only** (`documentType = DMER`, has a `documentRecordId`). Other document types
   (HRDIR reports, …) are not stored.
4. For each DMER:
   - **`driver` upsert** (only if Mercury supplied a driver with a **usable** licence — Mercury's
     placeholder `00000000` or an invalid licence means "no driver", see
     [S18](#s18-a-dmer-on-mercurys-placeholder-driver-00000000)):
     `INSERT … ON CONFLICT (licence_number) DO UPDATE SET mercury_driver_id, first_name, last_name, last_synced_at` → `driver_key`.
   - **`dmer_document` insert:** `INSERT … ON CONFLICT (document_guid) DO NOTHING` with
     `document_guid, document_name, mercury_document_status, document_priority, received_date,
     queue, business_area, mercury_case_id (from dmerDocumentCaseRecord), driver_key,
     document_url, pipeline_status='RECEIVED', current_stage='INGEST', attempt_count=0,
     first_seen_at, updated_at` (`dps_date` is always null). A re-polled DMER is left untouched.
   - **Publish to `dmer-ingest`** with `MessageId = document_guid` (duplicate detection drops repeats).
5. **Claim the whole page** — R2 `BatchSetDmerAIProccessedStatus` with **every** record's id,
   DMERs and non-DMERs, so none comes back. This is last on purpose: if it fails, the next tick
   re-reads the same records and step 4 is idempotent.
6. **`poll_checkpoint` upsert** (`source='BACKLOG'`, `last_run_at`).

Each DB call is its own transaction. A failure re-raises; the next tick retries.

### 6.2 Ingest Function — trigger `dmer-ingest`

1. Load the `dmer_document` row by `documentId`; no row → drop the message (logged).
2. **Replay guard:** if `pipeline_status != RECEIVED`, do nothing.
3. **`dmer_stage_run`** INSERT (`stage=INGEST, status=RUNNING, attempt_no`), closing any stale
   RUNNING row as FAILED/ABANDONED.
4. **Download the PDF** — R3 `GET Dmers/{documentRecordId}/files`.
5. **Blob** `raw-dmer/<yyyy>/<MM>/<document_guid>.pdf`.
6. **`dmer_document` UPDATE:** `raw_blob_url`, `pipeline_status='DOWNLOADED'`,
   `current_stage='EXTRACT'`, `attempt_count+1`.
7. **Publish to `dmer-raw`** (`blobUrl` = the raw PDF).
8. **`dmer_stage_run`** → SUCCEEDED (`output_blob_url` = raw PDF).

On an error in 4–8: stage run → FAILED (`error_code` = exception type), the message is redelivered
(5 tries) and then dead-lettered.

---

## 7. Stage 2 — Extraction (`di-processor`)

A long-running Container App reading `dmer-raw` one message at a time per replica (KEDA scales on
queue length, up to 10 replicas).

**Idempotent consume:** the message is claimed in `message_idempotency`
(scope `di-processor/dmer-raw`, 300 s lease). A completed claim → the message is just completed.
A handler failure dead-letters immediately (no redelivery retry).

**Replay routing** by current `pipeline_status`: NORMALIZED or later → complete, do nothing;
EXTRACTED → only re-publish to `dmer-extracted`; otherwise run the pipeline.

### 7.1 The pipeline (page 1 of the PDF)

| Step | What happens | Writes |
|---|---|---|
| 1 | `dmer_stage_run` start (`stage=EXTRACT`, `model_version = "di=<model>;prompt=<version>"`) | stage run |
| 2 | Download the PDF from `raw-dmer`; status RECEIVED→DOWNLOADED (if needed)→**EXTRACTING** (compare-and-set) | `dmer_document` |
| 3 | **A. Custom Document Intelligence model** (`DI_CUSTOM_MODEL_ID`, page 1, uncropped): every form field `{value, confidence}`; checkboxes → `"true"/"false"`. The same call's layout gives the **cut-off check**: header anchors in the top 20 % (2 of "DRIVER'S MEDICAL EXAMINATION", "RoadSafetyBC", "AREA ABOVE FOR OFFICE USE") and signature-block anchors in the bottom 40 % (or signature/date/fax fields located). `is_cutoff = not (has_header and has_signature)` | `top_level.json` |
| 4 | **B. OCR:** render page 1 at 300 DPI, **crop the top 25 %** (the PII band), cut into overlapping tiles (2 passes × 3 width segments), `prebuilt-read` on each (8 in parallel) | `ocr.json` |
| 5 | **C. Handwriting (Azure OpenAI):** one chat call with the page image + tiled OCR + the list of 65 handwritten fields. `high` confidence only when image and OCR agree (`source = both`), otherwise `low`. Sanitised: printed labels blanked, types coerced | `handwritten.json` |
| 6 | **D. Merge** (`merge.py`): start from the custom model's fields; for each handwritten field — visual acuity with custom confidence ≥ 0.95 keeps the custom value; else a `high` handwritten value wins; else (handwritten `low`) the custom value is kept if its confidence > 0.75, otherwise the handwritten value | `combined.json` |
| 7 | **Licence read:** custom-model field `dl_number`, normalized (7–8 digits → 8, else none) | — |
| 8 | **Comparison fingerprint:** two dates + ~110 checkboxes + other fields, lower-cased; `comparison_hash = sha256(...)` — used later to spot duplicates | — |
| 9 | **`dmer_extraction` upsert** (`ON CONFLICT (document_id) DO UPDATE`): `licence_number_read, has_header, has_signature, is_cutoff, comparison_fields, comparison_hash` (`page_count`, `exam_date`, … are null) | `dmer_extraction` |
| 10 | Status EXTRACTING → **EXTRACTED**, `current_stage = NORMALIZE` | `dmer_document` |
| 11 | **Publish `dmer-extracted`** (`blobUrl` = `combined.json`, deterministic `messageId`) | — |
| 12 | Stage run → SUCCEEDED (`output_blob_url` = `combined.json`) | stage run |

**On failure** (each step has a failure code: `PDF_UNREADABLE`, `DI_CUSTOM_MODEL_FAILED`,
`OCR_FAILED`, `LLM_CALL_FAILED`, `LLM_OUTPUT_INVALID`, `DB_*`, `PUBLISH_FAILED`, …): stage run →
FAILED, document → **MANUAL_REVIEW** (compare-and-set; `current_stage` shows where it stopped),
message dead-lettered with the failure code as its reason. The DLQ Drain takes it from there
([§10.4](#104-dlq-drain--timer-every-5-min)). A lost compare-and-set race (`STALE_STATUS`) just
stops quietly — another worker owns the document.

---

## 8. Stage 3 — Document orchestration (`workflow-orchestrator`)

**Trigger:** `StartDocumentOrchestration` on `dmer-extracted` (note: **deployed disabled** by
default — `documentOrchestrationTriggerEnabled=false` in Bicep). It starts the Durable
orchestration `DocumentOrchestration` with **instance id = `document_guid`**; if an instance is
already pending/running/completed, it does nothing (a failed one is restarted — the redrive path).

**Activity order (code):** `DriverLookup` → `NormalizeDmer` → `RunRuleEngine` → `SignalDriver`.
Each runs with Durable retry (first interval 30 s, 3 attempts). Each activity holds a Postgres
advisory lock on its document while it works and is **resumable**: a replay returns the committed
result without calling Mercury or the LLM again.

**Failure:** a "poison" error (validation/state) is not retried → `RouteToManualReview` with
`PERMANENT_BUSINESS`; retries exhausted → `RouteToManualReview` with `UNKNOWN`
(see [S12](#s12-a-document-orchestration-activity-fails)).

### 8.1 DriverLookup — who is the driver?

1. `start_or_resume` (lock `driver-lookup:<id>`): needs status EXTRACTED; a previous success
   returns straight away. `dmer_stage_run` INSERT (`stage=DRIVER_LOOKUP`), `attempt_count+1`.
2. **Which licence:**
   - Mercury supplied a driver at Ingest → use **that driver's licence**,
     `driver_resolved_by = MERCURY_SUPPLIED`; `licence_mismatch = true` if the licence read off
     the page differs.
   - Otherwise, a licence was read off the page → use it, `driver_resolved_by = LICENCE_LOOKUP`.
   - Otherwise → manual review `LICENCE_UNREADABLE` ([S9](#s9-the-driver-cant-be-identified)).
3. **Mercury R4 `POST drivers/lookup`.** 0 drivers → `DRIVER_NOT_FOUND`; more than 1 →
   `DRIVER_AMBIGUOUS` (both → [S9](#s9-the-driver-cant-be-identified)).
4. **`expected_document_count`** = the driver's documents of type DMER whose status is `Uploaded`
   (`MERCURY_COUNTED_DOCUMENT_TYPES`, `MERCURY_WAITING_DOCUMENT_STATUSES`). An initial estimate
   only.
5. **`attach_driver`, one transaction:**
   - stage run → SUCCEEDED;
   - `driver` upsert on `licence_number` (`mercury_driver_id, first_name, last_name, last_synced_at`);
   - `dmer_document`: `driver_key, driver_resolved_by, licence_mismatch` (status stays EXTRACTED);
   - `driver_evaluation`: open the driver's batch or attach to the open one —
     `INSERT (driver_key, open=true, expected_document_count, last_mercury_check_at) ON CONFLICT (driver_key) WHERE open DO UPDATE …`.

### 8.2 NormalizeDmer — turn extracted text into typed facts

- Reads `combined.json`; runs the normalization pipeline (`libs/dmer_common/normalization`):
  deterministic typing, dates, visual-acuity thresholds and guide checks around **Azure OpenAI**
  calls (categorise conditions, analyse each category, check evidence quotes) at temperature 0,
  schema-validated with up to 3 attempts. Evidence problems are flags, never rejections.
- **Blob** `normalized-dmer/<document_id>/runs/<run_id>/normalized.json`.
- **DB:** `dmer_stage_run` (`stage=NORMALIZE`, `model_version = <deployment>@normalization-schema-v3`,
  `output_blob_url`); `dmer_document` EXTRACTED → **NORMALIZED**, `current_stage = RULES`.

### 8.3 RunRuleEngine — the outcome for this DMER on its own

- Loads `rules/active/rules.json`, computes its sha256 and looks it up in `rules_version` (an
  unregistered ruleset stops the stage — registering is a deploy step).
- Evaluates it with **GoRules ZEN** against the normalized DMER (+ `received_date`). 21 decision
  tables (cerebrovascular, cardiovascular, diabetes, vision, priority, …) each propose outcomes;
  the **highest** wins by priority `CP < IN < PR < PU < TCM < CR`. Nothing matched → `IN`. A `CP`
  that needs a 5-year driving-record check becomes `IN`.
- **One transaction:** stage run → SUCCEEDED (`model_version = rules_version`); **`rule_evaluation`**
  INSERT (`selected_outcome_code, selected_reason, priority_rank, rules_version, all_outcomes,
  fit_letter, evaluated_at`); `dmer_document` NORMALIZED → **RULES_APPLIED**,
  `current_stage = DECISION`; **count the document into the driver's open batch**
  (`driver_evaluation_document` insert-once + `completed_document_count + 1`; if the driver's
  previous batch was already decided, a new open batch is created).

### 8.4 SignalDriver — wake the driver's decision

- `dmer_document` RULES_APPLIED → **AWAITING_DRIVER_COMPLETION** (compare-and-set).
- Publish to **`driver-decision`** with `SessionId = driver_key`,
  `MessageId = uuid5("driver-decision:<document_id>")`, `blobUrl` = the normalized blob.

---

## 9. Stage 4 — Driver orchestration and the decision (`driver-orchestrator`)

**Trigger:** `DriverDecision`, a **session** queue trigger on `driver-decision`. Because
`SessionId = driver_key`, one driver's signals are handled one at a time; different drivers run in
parallel. A second guard: a Postgres advisory lock `driver-decision:<driver_key>` (busy → redeliver).

`evaluate_driver` finds the driver's **open** `driver_evaluation`. None → nothing to do (the batch
was already decided); DECIDED/POSTED → nothing to do. Otherwise:

### 9.1 Step 1 — Is the batch complete?

1. Read the driver's licence from `driver`.
2. **Mercury R4 `POST drivers/lookup`** (fresh — a new DMER may have arrived).
3. **Waiting DMERs in Mercury** = the driver's documents of type DMER with status `Uploaded`
   **minus those we've already given an outcome** (`DriverDecisionRepository.already_decided`: any
   `UPDATE_OUTCOME` row in `mercury_outbox` for that `document_guid`). Mercury keeps a DMER
   `Uploaded` until a person reviews it, even after our outcome — without this subtraction, a
   driver's earlier AI-decided DMER would block the next batch forever.
4. **Batch** = the documents counted into this evaluation. `done` = those in
   AWAITING_DRIVER_COMPLETION + those in MANUAL_REVIEW.

| Check | Result |
|---|---|
| Mercury not reachable / no driver / several drivers | **WAITING** (`mercury_unverified`) → schedule a re-check ([S10](#s10-mercury-cant-be-checked-at-decision-time)) |
| A batch document still RULES_APPLIED | **WAITING** (`documents_still_processing`) — its own signal will re-trigger |
| `done < waiting DMERs` | **WAITING** (`waiting_for_documents`) — more DMERs to come |
| otherwise | **READY** → decide the AWAITING documents (MANUAL_REVIEW ones count as finished but aren't decided) |

WAITING writes `driver_evaluation.status = WAITING`, `expected_document_count`,
`last_mercury_check_at`.

### 9.2 Step 2 — Decide (pure function `decide()` in `decision/gateway.py`)

Applied in this order:

| Rule | Outcome |
|---|---|
| Attached medical report (`page_count > 2`) | `IN` — *currently never fires: `page_count` is always null* |
| No rule-engine outcome recorded | `IN` |
| **Every copy cut off** (or unknown) | every copy `IN`, "consider requesting a clearer copy" |
| Some clear, some cut off | decide on the clear copies; cut-off copies follow their result (`superseded_by_cutoff_rule`) |
| **Clear copies differ** (more than one `comparison_hash` group) | every copy `IN` ("Waiting DMERs for this driver differ"), with `differing_fields`; nothing rejected |
| **One DMER** | its rule-engine outcome |
| **Duplicates, all CP** (clean pass, I-19) | newest (by `received_date`) keeps `CP`; older copies `CP` + **is_duplicate** (rejected) |
| **Duplicates, not all CP** | every copy gets the **highest-priority** outcome of the group; nothing rejected |
| Driver was matched from the licence (`LICENCE_LOOKUP`) | a driver mapping is proposed (`proposed_driver_key`) → `MAP_DRIVER` |

The **Mercury comment** for each document:
`"AI outcome: <code>. Reason: <rule reason>. Fields that differ: … <notes…>"`.

### 9.3 Step 3 — Record the decision (`record_decided`, **one transaction**)

1. `driver_evaluation`: EVALUATING → **DECIDED**, `open = false`, `evaluated_at`,
   `decision_summary` (counts and ids only — no clinical text).
2. Re-check the batch didn't change meanwhile (else roll back and re-evaluate).
3. `dmer_document`: AWAITING_DRIVER_COMPLETION → **POSTING**, `current_stage = POST`.
4. **`dmer_decision`** INSERT per document: `outcome_code, is_duplicate, duplicate_of_document_id,
   duplicate_reason, superseded_by_cutoff_rule, driver_mapped, proposed_driver_key,
   decision_reason (jsonb), decided_by='AI', decided_at`.
5. **`mercury_outbox`** rows per document (status PENDING, due now,
   `idempotency_key = <dmer_decision.id>:<operation>`, `ON CONFLICT DO NOTHING`):

| Operation | When | Payload |
|---|---|---|
| `MAP_DRIVER` | driver matched from the licence | `document_guid, comment, mercury_driver_id, licence_number, first_name, last_name` |
| `CREATE_CASE` | always | `document_guid, comment, mercury_driver_id, licence_number, mercury_case_id` |
| `UPDATE_OUTCOME` | always | `document_guid, comment, outcome_code, decided_by='AI'` (+ `is_duplicate, duplicate_of_document_guid`) |

6. `dmer_stage_run` (`stage=DECISION`) → SUCCEEDED.

Because the decision and the intent to deliver it commit together, a decision can never exist
without someone trying to post it.

---

## 10. Stage 5 — Delivering the outcome, and the safety nets (`reliability`)

### 10.1 Outbox Publisher — timer every minute

1. **Claim** up to 20 documents with a due PENDING row, all their PENDING rows
   (`FOR UPDATE SKIP LOCKED`), leased 5 min (a crashed run's rows come back).
2. **Send each document's rows in order** `MAP_DRIVER` → `CREATE_CASE` → `UPDATE_OUTCOME`:

   - **`MAP_DRIVER`** → W2 `add_driver`.
   - **`CREATE_CASE`** →
     0. **the AI linked the driver** (`MAP_DRIVER` sent; the publisher saved `add_driver`'s
        `driverOpenCase` on this row): a real active case → nothing sent; no active case, or the
        placeholder driver's case (title starting with `00000000`) → W3 `add_case`;
     1. DMER arrived with a case (`mercury_case_id`) → nothing sent (`skipped: dmer_has_case`);
     2. else R4 `drivers/lookup` (from the write base): the driver has an open case → nothing sent
        (`skipped: driver_has_open_case`; Intake attaches it);
     3. else W3 `add_case`.
   - **`UPDATE_OUTCOME`** → W1 `SetDmerAIProccessedStatus` (body per the table in §5.3).

3. **Record:** success → `SENT, sent_at, mercury_response`. Failure →
   - retryable → back off (1, 2, 4, 8, … 60 min) and the document's later rows wait;
   - rejected, or 10 attempts → **FAILED** (one ERROR log = the alert); later rows become FAILED
     "blocked" — **except the outcome**, which still goes:
     - after a failed `MAP_DRIVER`: as a processing **Error** to Team - Intake, error in the notes;
       `CREATE_CASE` is blocked;
     - after a failed `CREATE_CASE`: as normal, the case error added to the notes.
4. **Roll-up:** all rows SENT → document **COMPLETED**; driver link failed but outcome SENT →
   document **MANUAL_REVIEW** (Intake has it); a batch whose documents are all finished either way
   → evaluation **POSTED**.

### 10.2 Reconciliation Sweeper — timer every 15 min

| Query | Finds | Does |
|---|---|---|
| Stalled documents | in RECEIVED…NORMALIZED with `updated_at` older than the stage SLA (30 min) | re-publish to the queue that status waits on (`attempt_count+1`, `processing_error` TRANSIENT_STALLED); after 3 → **MANUAL_REVIEW** (UNKNOWN_STALLED) |
| Stale batches | open WAITING evaluations whose documents are all through rules but nothing moved within the SLA | WAITING → **STALE**, re-signal `driver-decision` (the orchestrator takes STALE → READY) |
| Outbox backlog | PENDING rows overdue or retried ≥ 5 times | ERROR log for the alert (no writes) |

### 10.3 Daily reconciliation — once a day

Reads one page of R1 (it can only see records **not yet claimed**) and checks each against our
tables: `NOT_RECEIVED`, `OVERDUE`, `MANUAL_REVIEW_NO_OUTCOME`, `NOT_POSTED`, `POST_FAILED`. Adds
24-hour metrics (manual reviews, fallbacks, stage p95). Saves `reconciliation-reports/<date>.json`.

### 10.4 DLQ Drain — timer every 5 min

Reads the dead-letter sub-queues of `dmer-ingest`, `dmer-raw`, `dmer-extracted`,
`driver-decision`; **classifies** each dead letter (from its reason, or the document's last stage
error):

| Category | Examples | Action |
|---|---|---|
| **PERMANENT_BUSINESS** | `PDF_UNREADABLE`, document gone from Mercury | document → MANUAL_REVIEW **and a fallback outcome is queued**: `UPDATE_OUTCOME` IN, `decided_by=FALLBACK`, "Unable to AI process due to error. The DMER PDF could not be read." (key `<document_id>:FALLBACK:UPDATE_OUTCOME`) |
| TRANSIENT / PROCESSING | DB, storage, publish, dependency 5xx/429, lock lost | **redrive**: reopen the document to the queue's entry status and re-send with backoff (5, 10, 20 min), up to 3 times |
| UNKNOWN (or redrives exhausted) | logic errors, unclassified | document → MANUAL_REVIEW for a person (no outcome) |

Every drained message writes a `processing_error` row (idempotent per dead-letter message id).

---

## 11. Scenario catalogue

Each scenario lists what happens, the DB writes that differ from the happy path, and the Mercury
calls.

### S1. One DMER, Mercury already knows the driver — the happy path

1. Poll: R1 reads it; `driver` upsert; `dmer_document` RECEIVED (`driver_key` set,
   `mercury_case_id` set if it has a case); `dmer-ingest`; R2 claims it (Mercury: InProgress).
2. Ingest: R3 downloads; `raw-dmer` blob; DOWNLOADED; `dmer-raw`.
3. Extraction: four blobs; `dmer_extraction`; EXTRACTED; `dmer-extracted`.
4. DriverLookup: `MERCURY_SUPPLIED`; R4; batch opened (`driver_evaluation` WAITING,
   `expected_document_count = 1`).
5. Normalize → NORMALIZED. RuleEngine → `rule_evaluation` (say `PR`); RULES_APPLIED; counted into
   the batch. SignalDriver → AWAITING_DRIVER_COMPLETION; `driver-decision`.
6. Gateway: R4 → 1 waiting, batch has 1 finished → READY → `single_document`, `PR`.
   `dmer_decision`; outbox `CREATE_CASE` + `UPDATE_OUTCOME`; evaluation DECIDED; document POSTING.
7. Publisher: `CREATE_CASE` skipped (DMER has a case); W1 outcome PR → Team - Adjudicators,
   Regular, Adjudicate. Document **COMPLETED**, evaluation **POSTED**.

### S2. The driver is found from the licence on the page

Mercury's batch had no driver (or the placeholder driver `00000000`). DriverLookup uses the
licence read off the page (`LICENCE_LOOKUP`), R4 finds the driver, `driver` is upserted with
Mercury's names. The gateway proposes the mapping → outbox gets **`MAP_DRIVER`** too.
Publisher: W2 `add_driver` first, then `CREATE_CASE` (likely `add_case` if no case exists), then
W1. If `add_driver` fails for good → [S14c](#s14c-add_driver-fails-for-good).

### S3. Several DMERs for the same driver arrive over time

Each document runs stages 1–3 independently and each SignalDriver wakes the driver. The gateway
answers WAITING (`documents_still_processing` or `waiting_for_documents`) until every `Uploaded`
DMER Mercury holds for the driver is AWAITING in our batch; the last signal finds it READY and the
batch is decided together. Signals for one driver are serialized by the session.

### S4. Duplicate copies, all clean pass (CP)

Same `comparison_hash` in one batch, every copy `CP` → the **newest** (by `received_date`) keeps
`CP`; older copies `CP` with `is_duplicate = true`, `duplicate_of_document_id = newest`.
Outbox for a rejected copy: `UPDATE_OUTCOME` with `is_duplicate: true` → W1 sends only
`aiIsDuplicate: true` + "Duplicate of DMER ‹guid›.".

### S5. Duplicate copies with different rule outcomes

Same `comparison_hash`, outcomes differ (e.g. `CP` and `PU`) → **every** copy gets the
highest-priority outcome (`PU`); nothing is rejected; note "Duplicate copies received different
rule engine outcomes; the highest-priority outcome was applied and no copy was rejected."

### S6. The driver's waiting DMERs differ

More than one `comparison_hash` group among the clear copies → every copy `IN` with
`differing_fields` listed in the comment; nothing rejected (I-6 "newest wins" is not implemented).

### S7. A cut-off scan

`is_cutoff` true (header or signature block missing). All copies cut off → every copy `IN`,
"consider requesting a clearer copy". A clear copy exists → it decides; cut-off copies follow with
`superseded_by_cutoff_rule = true` (and are marked duplicates on a clean pass).

### S8. A re-fax arrives after the first copy was decided (I-10)

The first copy's batch is closed (DECIDED/POSTED). The re-fax's RuleEngine opens a **new** batch.
At the gateway, Mercury still shows the first copy `Uploaded` (Intake hasn't reviewed it), but its
`document_guid` has an `UPDATE_OUTCOME` row → **skipped** from the waiting count → the re-fax is
decided on its own (no cross-batch duplicate marking).

### S9. The driver can't be identified

DriverLookup ends in **`LICENCE_UNREADABLE`** (no licence read), **`DRIVER_NOT_FOUND`** (R4 returns
no driver) or **`DRIVER_AMBIGUOUS`** (several). One transaction:

- stage run FAILED (`error_code` = the reason);
- `dmer_document` EXTRACTED → **MANUAL_REVIEW**;
- `processing_error` (`DRIVER_LOOKUP`, PERMANENT_BUSINESS, the reason);
- **`mercury_outbox` `UPDATE_OUTCOME`**: `outcome_code IN, decided_by FALLBACK`, comment
  "Unable to AI process due to error. The driver's file could not be found or mapped in Mercury.
  ‹reason› Licence read from the DMER: ‹licence›." (key `<document_id>:FALLBACK:UPDATE_OUTCOME`).

No `dmer_decision`, no driver created, no batch. Publisher → W1 as **Error**, Team - Intake.

### S10. Mercury can't be checked at decision time

R4 fails (outage) or returns no/several drivers → WAITING `mercury_unverified`. The orchestrator
**schedules re-checks** on `driver-decision` at 15, 30, 60, 120, 240 min (6 checks, ~8 h). After
the last one → `route_batch_to_manual_review`: evaluation → DECIDED (closed); the batch's
documents → MANUAL_REVIEW; per document a `processing_error` (`DecisionGateway:MERCURY_UNVERIFIED`),
a **`dmer_decision` `IN`, `decided_by=FALLBACK`**, and an outbox `UPDATE_OUTCOME` fallback →
W1 **Error** to Team - Intake.

### S11. Extraction fails

di-processor marks the document **MANUAL_REVIEW** and dead-letters the message with the failure
code. DLQ Drain:
- `PDF_UNREADABLE` → PERMANENT_BUSINESS → fallback outcome queued → W1 **Error** ("The DMER PDF
  could not be read.").
- DB / storage / publish / dependency outage → redrive (reopen to DOWNLOADED, re-send with
  backoff, up to 3 times) → afterwards UNKNOWN → MANUAL_REVIEW.
- Model output invalid / logic errors → see the category table in §10.4.

### S12. A document-orchestration activity fails

Poison error (bad input/state) → `PERMANENT_BUSINESS`; retries exhausted → `UNKNOWN`.
`RouteToManualReview` (one transaction): document → MANUAL_REVIEW; if it has a driver, it's
counted into the batch (so the batch doesn't wait for it); `processing_error`; then a
`driver-decision` signal. **No fallback outcome is queued** — see [§13](#13-known-gaps-and-open-questions).

### S13. Work stops moving

The Sweeper re-publishes stalled documents (up to 3 times, then MANUAL_REVIEW) and re-signals
stale WAITING batches. The Ingest gap — DOWNLOADED written but the `dmer-raw` publish lost — is
covered this way (re-published after the 30-min SLA).

### S14. Mercury write failures

**S14a — Mercury is down.** Rows back off (1 → 60 min); the document's later rows wait; after
10 attempts → FAILED (alert). The Sweeper alerts on overdue PENDING rows meanwhile.

**S14b — The outcome is rejected** (4xx or `errorStatus`). `UPDATE_OUTCOME` FAILED (alert); the
document stays POSTING; reconciliation shows `POST_FAILED`. A person resolves it in Mercury.

**S14c — `add_driver` fails for good.** `MAP_DRIVER` FAILED; `CREATE_CASE` FAILED "blocked";
`UPDATE_OUTCOME` gets `driver_link_error` on its payload and is sent as **Error**, Team - Intake,
notes "Unable to AI process due to error: the driver could not be linked to the DMER (‹error›).
‹AI comment›". Document → **MANUAL_REVIEW**; the batch can still reach POSTED.

**S14d — `add_case` fails for good** (404 driver not found, 409). `CREATE_CASE` FAILED;
`UPDATE_OUTCOME` gets `case_error` and is sent **normally**, notes + "The case could not be
created for this DMER (‹error›)." Document stays **POSTING**, evaluation DECIDED (a person
resolves the case).

### S15. Case handling

**After the AI linked the driver (`add_driver`)** — decided from its `driverOpenCase`:

| `add_driver` returned | What the publisher does |
|---|---|
| an active case with a real title | nothing (`driver_has_open_case`) |
| the placeholder driver's case (title starting with `00000000`) — not expected for a real licence (to confirm with Mercury) | W3 `add_case` |
| no active case | W3 `add_case` |

**Otherwise:**

| DMER has a case (from the batch) | Driver has an open case (live lookup) | What the publisher does |
|---|---|---|
| yes | — | nothing (`dmer_has_case`) |
| no | yes | nothing (`driver_has_open_case`) — Intake attaches it |
| no | no | W3 `add_case` (`driver_id` = licence) |

### S16. Non-DMER records on the page

HRDIR reports and other document types are **claimed** (R2, so they never come back and leave
the DPS queues) but not stored or processed. In Mercury they stay `InProgress`.

### S17. Replays and duplicates of messages

Every stage is idempotent: `dmer_document` insert-once on `document_guid`; the Ingest status
guard; di-processor's `message_idempotency` + status compare-and-set; Durable instance id =
`document_guid` and resumable activities; insert-once batch markers; deterministic message ids;
outbox `idempotency_key` uniqueness and the `Idempotency-Key` header.

### S18. A DMER on Mercury's placeholder driver (`00000000`)

Mercury files a DMER its own systems couldn't map to a driver under a **placeholder driver**
(licence `00000000`, surname "DFC"), on the placeholder's case (title `00000000- DFC- DMER- n`).

1. **Ingest:** `00000000` is not a licence (`PLACEHOLDER_LICENCE`; `normalize_licence` returns none,
   `is_placeholder_licence`), so **no `driver` row and no `driver_key`** — the DMER is stored as
   having no driver (`mercury_case_id` keeps the placeholder's case id). An invalid licence on a
   Mercury driver is treated the same way instead of failing the poll.
2. **Driver Lookup:** no driver supplied → the **licence read off the page** (`LICENCE_LOOKUP`)
   → R4 finds the real driver; its own batch. Zeros read off the page count as unreadable →
   [S9](#s9-the-driver-cant-be-identified).
3. **Decision:** the mapping is proposed → outbox `MAP_DRIVER`, `CREATE_CASE`, `UPDATE_OUTCOME`.
4. **Publisher:** W2 `add_driver` links the real driver and returns `driverOpenCase`; the case
   write follows [S15](#s15-case-handling)'s first table (create a case unless the driver has a
   real active case); W1 sends the outcome. If `add_driver` fails →
   [S14c](#s14c-add_driver-fails-for-good).

---

## 12. Where the stage docs and the code disagree

The code is right; these stage docs are behind it:

- **Activity order:** Driver Lookup runs **first**, not after the Rule Engine (03).
- **Driver orchestration** is a session-triggered Function with self-scheduled Mercury re-checks,
  not a Durable orchestrator; the lock is `pg_try_advisory_lock('driver-decision:<driver_key>')` (06).
- **Decision Gateway** (07): it **does** write `dmer_decision`, documents go AWAITING → **POSTING**
  (never DECIDED); duplicates keep the **newest by `received_date`** and are rejected **only when
  all CP**; cut-off content comparison (I-4) and "newest wins" for differing DMERs (I-6) are not
  implemented; the attached-report and no-rule-outcome rules exist but aren't documented.
- **Fallback writers:** not only the DLQ Drain — Driver Lookup (S9) and the gateway's
  manual-review route (S10) also queue fallback outcomes; only the gateway writes a FALLBACK
  `dmer_decision`; the DLQ fallback key is `<document_id>:FALLBACK:UPDATE_OUTCOME`.
- **Extraction** (02): OCR is `prebuilt-read` over the whole cropped page (not region-targeted
  `prebuilt-ocr`); `page_count`, `exam_date`, `physician_name`, `confidence_avg` are not written;
  there is no Service Bus lock renewal.
- **Ingest** (01): `dps_date` is never written; the checkpoint is an upsert of the poll time.
- **Message bodies** are camelCase on the wire (docs show snake_case).
- **Env var names** in several stage docs are stale — use [§14](#14-configuration-cheat-sheet).
- `data-model.md` lists `dmer_decision.fallback_reason_code` and a unique index on
  `dmer_decision(document_id)` that the migrations don't create.

## 13. Known gaps and open questions

| Gap | Effect | Status |
|---|---|---|
| `RouteToManualReview` (S12) queues **no** fallback outcome | The DMER is claimed (InProgress in Mercury) but Intake never gets an outcome — only the daily report's `MANUAL_REVIEW_NO_OUTCOME` would show it, and that report can't see claimed records | Open |
| Daily reconciliation reads the claim-based `GET Dmers` | It can't see claimed records — needs a "get DMER by id" or non-claiming read from Mercury | Open with Mercury |
| `page_count` is never written | The attached-report rule (> 2 pages → IN) never fires | Open |
| An invalid licence on a Mercury-supplied driver at Ingest raised before the claim | That page was re-read and failed every tick | **Fixed 2026-11-23** — treated as no driver (S18) |
| No lock renewal on `dmer-raw` | An extraction over 5 min can lose its lock (compare-and-set keeps it correct) | Open |
| Non-DMER records stay InProgress in Mercury | — | Asked Mercury/Intake |
| `add_case` 409s ("different driver" vs "already has a case") can't be told apart | "already has a case" shows as a failed write and a misleading note | Asked Mercury |
| Idempotency of Mercury writes (M-6) | The publisher retries after timeouts | Asked Mercury |
| Failure-path design (failed driver link → MANUAL_REVIEW; failed case → POSTING) | — | Architect review |
| Document orchestration trigger deploys disabled | Enable it per environment | By design |

## 14. Configuration cheat sheet

| Setting | App(s) | Default / example | Meaning |
|---|---|---|---|
| `MERCURY_AUTH_TOKEN_URL`, `MERCURY_API_KEY` | all Mercury callers | — | Token endpoint and secret (Key Vault) |
| `MERCURY_API_BASE_URL` | intake, reliability | `https://<host>/Dmers` | R1–R3 |
| `MERCURY_DRIVER_LICENCE_API_BASE_URL` | workflow-, driver-orchestrator | `https://<host>/drivers` | R4 |
| `MERCURY_WRITE_API_BASE_URL` | reliability | `https://<host>` | W1–W3 and the case check |
| `MERCURY_PAGE_SIZE` | intake, reliability | 50 | Page size |
| `MERCURY_RECEIVED_DATE_TIMEZONE` | intake | America/Vancouver | Zone for `receivedDate` without offset |
| `MERCURY_COUNTED_DOCUMENT_TYPES` | orchestrators | DMER | Types counted as a driver's DMERs |
| `MERCURY_WAITING_DOCUMENT_STATUSES` | orchestrators | Uploaded | Statuses that mean "still waiting" |
| `MERCURY_TOKEN_TTL_SECONDS` | all | 1800 | Token lifetime when Mercury gives none |
| `DMER_POLL_SCHEDULE` | intake | `0 */5 * * * *` | Poller timer |
| `DMER_INGEST_QUEUE`, `DMER_RAW_QUEUE`, `DMER_EXTRACTED_QUEUE`, `DRIVER_DECISION_QUEUE` | various | queue names | |
| `SERVICE_BUS_NAMESPACE_FQDN`, `ServiceBusConnection__fullyQualifiedNamespace` | all | — | Sends / triggers (Managed Identity) |
| `POSTGRES_HOST`, `POSTGRES_USER`, `POSTGRES_DATABASE`, `POSTGRES_PASSWORD` | all | — | Password only locally; Entra token in Azure |
| `DMER_RAW_CONTAINER`, `BLOB_ACCOUNT_URL`, `EXTRACTED_DMER_CONTAINER`, `NORMALIZED_DMER_CONTAINER`, `RULES_CONTAINER`, `RULES_ACTIVE_PATH` | various | raw-dmer, extracted-dmer, normalized-dmer, rules, active/rules.json | Storage |
| `DOC_INTELLIGENCE_ENDPOINT`, `DI_CUSTOM_MODEL_ID` | di-processor | — | Document Intelligence |
| `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION` | di-processor, workflow-orchestrator | 2024-10-21 | Azure OpenAI |
| `DECISION_ENGINE` | workflow-orchestrator | rules | `rules` or `triage_llm` (parked) |
| `ORCHESTRATION_RETRY_FIRST_INTERVAL_SECONDS`, `ORCHESTRATION_RETRY_MAX_ATTEMPTS` | workflow-orchestrator | 30, 3 | Activity retry |
| `OUTBOX_PUBLISHER_SCHEDULE`, `OUTBOX_BATCH_SIZE`, `OUTBOX_MAX_ATTEMPTS`, `OUTBOX_BACKOFF_INITIAL_SECONDS`, `OUTBOX_BACKOFF_MAX_SECONDS`, `OUTBOX_LEASE_SECONDS` | reliability | every minute, 20, 10, 60, 3600, 300 | Publisher |
| `SWEEPER_SCHEDULE`, `STAGE_SLA_*_MINUTES`, `SWEEPER_MAX_REPUBLISH` | reliability | every 15 min, 30, 3 | Sweeper |
| `DLQ_DRAIN_SCHEDULE`, `DLQ_REDRIVE_MAX_ATTEMPTS`, `DLQ_REDRIVE_BACKOFF_SECONDS` | reliability | every 5 min, 3, 300 | DLQ Drain |
| `RECONCILIATION_SCHEDULE` | reliability | `0 0 13 * * *` | Daily report |

## 15. Where to look in the code

| Topic | Code |
|---|---|
| Mercury client (all calls, token, retries) | `libs/dmer_common/src/dmer_common/mercury_client/` (`__init__.py`, `auth.py`, `ai_status.py`, `driver_link.py`, `case_link.py`) |
| Page Poller, Ingest | `services/intake-processor/function_app.py` |
| Extraction | `services/di-processor/src/di_processor/` (`pipeline.py`, `extraction/*`) |
| Document orchestration | `services/workflow-orchestrator/src/workflow_orchestrator/` (`orchestrators/`, `activities/`) |
| Normalization, rules | `libs/dmer_common/src/dmer_common/normalization/`, `rules/`; ruleset `services/rule-engine/rules/rules.json` |
| Driver decision | `services/driver-orchestrator/src/driver_orchestrator/` (`evaluate.py`, `decision/`) |
| Outbox rows, publisher | `libs/dmer_common/src/dmer_common/db/mercury_outbox.py`, `outbox_publisher.py` |
| Sweeper, reconciliation, DLQ | `libs/dmer_common/src/dmer_common/db/sweeper.py`, `reconciliation.py`, `dlq_drain.py`, `dlq.py`; timers in `services/reliability/` |
| Status machine | `libs/dmer_common/src/dmer_common/db/status.py` |
| Schema | `database/migrations/` |
| DEV mock of Mercury | `services/intake-processor/tests/local/mock-mercury-function-app/function_app.py` |
| Design rationale per stage | `docs/development/stages/*.md` |
