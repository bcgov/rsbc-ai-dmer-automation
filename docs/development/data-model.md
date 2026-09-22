# Data Model Reference

Canonical reference for every PostgreSQL table in the DMER Intake Automation pipeline, derived
from the revised architecture (`docs/architecture/DMER_Intake_Automation_Revised_Architecture.docx`,
§7) and `docs/architecture/Figure2_Database_ERD.png` / `Figure3_Status_Lifecycle.png`.

**This supersedes** the `documents` table and `DocumentStatus` enum currently implemented in
`libs/dmer_common/src/dmer_common/db/documents.py` and `status.py`. Those model a five-state
`di-processor`-only lifecycle (`received → extracting → sectioning → combining → combined →
published`) from the original architecture. See [Alignment gaps](#alignment-gaps-vs-current-code)
below.

Every stage document under `docs/development/stages/` links back here instead of repeating table
definitions. Do not fork column lists into a stage doc — if a stage needs a column not listed
here, add it here first.

## Design principles (carried into every table)

- **Mercury stays the system of record.** Postgres is the system of record for *processing state,
  artifacts, decisions and audit* only. No column here replaces a Mercury field; several columns
  cache a Mercury value that gets refreshed at specific, documented points (never on every poll).
- **Messages carry pointers, not payloads.** No table stores an extracted/normalized document body
  as a queue payload; blob URLs are stored instead. See `message-contracts.md`.
- **Every stage is replayable.** Each stage's blob output is versioned by path, and `dmer_stage_run`
  records the URL so a stage can be re-run from its predecessor's output.
- **No implicit terminal states.** "Nothing to do yet" and "waiting on siblings" are rows
  (`pipeline_status = AWAITING_DRIVER_COMPLETION`, `driver_evaluation.status = WAITING`), not a
  function that returns without writing anything.
- **Stage status lives in an audit table, not widening columns.** `dmer_stage_run` is one row per
  document per stage per attempt. `dmer_document` carries only two denormalized pointers
  (`current_stage`, `pipeline_status`) for cheap current-state queries.

## Table reference

### `dmer_document`

One row per `document_guid` (Mercury's identifier). Mercury metadata as last received, plus the
document's current position in the pipeline.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | Internal identifier — use this, never `document_guid`, in queue messages and joins. |
| `document_guid` | uuid, **UNIQUE** | Mercury's identifier. See [`document_guid` is not a content key](#document_guid-is-not-a-content-key) below — this uniqueness guards against redelivery, not duplicate content. |
| `document_name` | text | As received from Mercury. |
| `mercury_document_status` | text | Mercury's own status field (`Uploaded`, `Rejected`, ...). Refreshed only by the driver orchestration's completeness call ([Decision Gateway](stages/07-decision-gateway.md)), never by the poller. |
| `document_priority` | text | From Mercury. Per question I-3, does **not** override the rule engine's PR/PU/PCM/CR selection. |
| `received_date` / `dps_date` | timestamptz | `dps_date` empty = "not yet triaged" (question I-9, confirmed reliable signal). Refreshed at decision time only. |
| `queue` / `business_area` | text | DPS General / DPS Unknown, etc. |
| `mercury_case_id` | text, nullable | Set when Mercury supplied a case. |
| `driver_key` | uuid FK → `driver.driver_key`, nullable | Null until Mercury supplies a driver object or [Extraction](stages/02-extraction.md) resolves one from the page. |
| `raw_blob_url` | text | Set by Ingest once the source PDF lands in `raw-dmer`. |
| `pipeline_status` | enum | Health/lifecycle state — see [Status modelling](#status-modelling). |
| `current_stage` | enum | Position — see [Status modelling](#status-modelling). |
| `attempt_count` | int | Incremented on republish (sweeper) or stage retry. |
| `correlation_id` | uuid | Generated once at ingest; constant for the document's life; propagate on every log line and queue message. |
| `first_seen_at` / `updated_at` | timestamptz | `updated_at` is set on **every** write to this row, by every stage — it is what the reconciliation sweeper's stall-detection query scans. |

Unique on `document_guid` — this is what makes re-polling and webhook overlap safe (idempotent
upsert; see [Ingest](stages/01-ingest.md)).

### `dmer_stage_run`

The audit trail. One row per document, per stage, per attempt. **Every stage writes one of these**
(insert `RUNNING` at start, update to `SUCCEEDED`/`FAILED` at end) — this convention is not
repeated in each stage doc.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | uuid FK → `dmer_document.id` | |
| `stage` | enum | `INGEST`, `EXTRACT`, `NORMALIZE`, `RULES`, `DECISION`, `POST` (see stage docs for exact value per stage). |
| `status` | enum | `RUNNING`, `SUCCEEDED`, `FAILED`. |
| `attempt_no` | int | |
| `started_at` / `ended_at` | timestamptz | |
| `output_blob_url` | text, nullable | The blob this attempt produced, if any. |
| `model_version` | text, nullable | DI custom model version, GPT prompt/schema version, or rules version — whichever applies to the stage. |
| `error_code` / `error_detail` | text, nullable | Set on `FAILED`. |

Index: `(document_id, stage, attempt_no DESC)` — the replay/timing query.

### `driver`

The grouping key, independent of Mercury, because documents must be grouped by licence even when
Mercury returns no driver object.

| Column | Type | Notes |
|---|---|---|
| `driver_key` | uuid PK | Internal identifier. **Never put the licence number on a queue message** — use `driver_key` (security requirement, §9.2). |
| `licence_number` | text, **UNIQUE** | Normalized: uppercase, punctuation stripped, before the uniqueness check. |
| `mercury_driver_id` | text, nullable | |
| `first_name` / `last_name` | text | |
| `last_synced_at` | timestamptz | |

`INSERT ... ON CONFLICT (licence_number) DO UPDATE` — written by both Ingest (when Mercury supplies
a driver object) and Extraction (when a licence is read from the page and no row exists yet).

### `driver_evaluation`

The join unit — the row that makes "waiting on siblings" explicit and queryable.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `driver_key` | uuid FK → `driver.driver_key` | |
| `status` | enum | `WAITING`, `STALE`, `READY`, `EVALUATING`, `DECIDED`, `POSTED` — see [Status modelling](#status-modelling). |
| `expected_document_count` | int | Set during Extraction from the Mercury `GET by driver_licence` call; **re-verified** at decision time (question: a new document may have arrived mid-wait). |
| `completed_document_count` | int | Incremented by the rule-engine activity each time a sibling document reaches `RULES_APPLIED`. |
| `last_mercury_check_at` | timestamptz | |
| `evaluated_at` | timestamptz, nullable | |
| `decision_summary` | jsonb | How many documents, how many duplicates, how many cut off, which document was retained, which rule path was taken. |

`INSERT ... ON CONFLICT (driver_key, open) DO UPDATE` per the architecture doc — **the meaning of
"open" is not fully specified**; see [Open Questions](#open-questions--decisions-required).

### `dmer_extraction`

One row per document (1:1 with `dmer_document`) — the extracted values that need to be *queried*,
not merely stored (the full extraction JSON lives in the `extracted-dmer` blob).

| Column | Type | Notes |
|---|---|---|
| `document_id` | uuid PK, FK → `dmer_document.id` | |
| `licence_number_read` | text, nullable | As read from the page (may disagree with Mercury's driver object). |
| `exam_date` | date, nullable | |
| `physician_name` | text, nullable | |
| `has_header` / `has_signature` / `is_cutoff` | bool | Three separate flags, deliberately not collapsed — Intake needs to know which half of the form is missing. |
| `page_count` | int | |
| `confidence_avg` | numeric | |
| `comparison_fields` | jsonb | The canonicalized subset used for duplicate detection (trimmed, lowercased, normalized dates/numbers). |
| `comparison_hash` | char(64) | sha256 of `comparison_fields`. Indexed — this is what makes duplicate detection a hash lookup instead of a blob diff. |

### `rule_evaluation`

One row **per evaluation, not per document** — a re-run creates a new row; nothing is overwritten.

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | uuid FK → `dmer_document.id` | |
| `rules_version` | text FK → `rules_version.version` | |
| `all_outcomes` | jsonb | Every candidate outcome the engine returned, with its inputs — not just the winner. |
| `selected_outcome_code` | text | |
| `selected_reason` | text | |
| `priority_rank` | int | |
| `evaluated_at` | timestamptz | |

### `rules_version`

One row per published `rules.json`, so a past decision can be reproduced exactly.

| Column | Type | Notes |
|---|---|---|
| `version` | text PK | |
| `blob_url` | text | Points into the `rules` container (see `azure-blob-storage.md`). |
| `checksum` | char(64) | |
| `activated_at` / `activated_by` | timestamptz, text | |

### `dmer_decision`

The final per-document outcome. Written **once, atomically with the outbox row**, by
[Post-Processing](stages/08-post-processing.md) — never by the Decision Gateway directly.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `document_id` | uuid FK → `dmer_document.id` | |
| `driver_evaluation_id` | uuid FK → `driver_evaluation.id` | |
| `outcome_code` | enum | `CP`, `IN`, `PR`, `PU`, `PCM`, `CR` — Mercury/business outcome codes. **Definitions are not in the architecture document**; source them from Intake/business-rules documentation before building the rule engine's outcome table. Flagged in [Open Questions](#open-questions--decisions-required). |
| `is_duplicate` | bool | |
| `duplicate_of_document_id` | uuid FK → `dmer_document.id`, nullable | Self-referencing via `dmer_decision`; see the `duplicate_of` edge on the ERD. |
| `duplicate_reason` | text, nullable | Human-readable, for Intake to inspect when they disagree. |
| `superseded_by_cutoff_rule` | bool | Set when a cut-off document was excluded from the outcome decision in favour of a clear sibling. |
| `driver_mapped` | bool | |
| `proposed_driver_key` | uuid, nullable | Set when Mercury had no driver object but a licence resolved to exactly one driver (question I-11: AI may map automatically). |
| `decision_reason` | jsonb | The rule path, the diff that forced `IN`, the cut-off flags that applied. |
| `decided_by` | enum | `AI`, `FALLBACK`, `MANUAL` — a fallback outcome must never be mistaken for a considered one. |
| `decided_at` | timestamptz | |

Duplicates and outcomes are recorded here, **not** on `dmer_document` — a document is a fact; being
a duplicate is a decision, and decisions can change when a new sibling arrives (question I-10).

### `mercury_outbox`

The delivery guarantee. One row per intended Mercury operation, written in the **same transaction**
as `dmer_decision`.

| Column | Type | Notes |
|---|---|---|
| `id` | uuid PK | |
| `document_id` | uuid FK → `dmer_document.id` | |
| `operation` | enum | `UPDATE_OUTCOME`, `MARK_DUPLICATE`, `MAP_DRIVER`, `CREATE_CASE` — see `message-contracts.md` for payload shape per operation. |
| `payload` | jsonb | |
| `idempotency_key` | text, **UNIQUE** | Sent to Mercury on every attempt so a retried POST/PUT is safe (pending question M-6: does Mercury actually honour an idempotency key?). |
| `status` | enum | `PENDING`, `SENT`, `FAILED`. |
| `attempt_count` | int | |
| `next_attempt_at` | timestamptz | Exponential backoff schedule. |
| `last_error` | text, nullable | |
| `sent_at` | timestamptz, nullable | |
| `mercury_response` | jsonb, nullable | |

### `processing_error`

The failure register — populated by the [DLQ Drain](stages/10-dlq-drain.md) and by handled errors
(e.g. the reconciliation sweeper detecting a stall).

| Column | Type | Notes |
|---|---|---|
| `id` | bigserial PK | |
| `document_id` | uuid FK → `dmer_document.id` | |
| `stage` | enum | Same stage enum as `dmer_stage_run`. |
| `error_class` | enum | `TRANSIENT`, `POISON`, `DOWNSTREAM` — see `azure-service-bus.md` for the classification rule. |
| `message` | text | |
| `dlq_message_id` | text, nullable | |
| `occurred_at` | timestamptz | |
| `resolved_at` / `resolution` | timestamptz, text, nullable | |

### `poll_checkpoint`

Where the poller got to, per source (`BACKLOG` or `REALTIME`).

| Column | Type | Notes |
|---|---|---|
| `source` | text PK | |
| `last_page` | int | Or the opaque cursor from Mercury's cursor-based pagination (question M-2) — confirm the type once the poller is built; the architecture doc's ERD shows `int` but Mercury returns an opaque `cursor` string. |
| `last_received_date` | timestamptz | |
| `last_run_at` | timestamptz | |

## Status modelling

Two orthogonal fields on `dmer_document`, not one 20-value enum — `current_stage` says *where* a
document is, `pipeline_status` says *how it is doing*.

### `dmer_document.pipeline_status` (document lifecycle)

```
RECEIVED → DOWNLOADED → EXTRACTING → EXTRACTED → NORMALIZED → RULES_APPLIED
    → AWAITING_DRIVER_COMPLETION → DECIDED → POSTING → COMPLETED
```

`MANUAL_REVIEW` is reachable from `EXTRACTING`, `NORMALIZED` (retries exhausted), or any stage via
poison/DLQ. `AWAITING_DRIVER_COMPLETION` is an **explicit row**, never an implicit gap — it is
what the reconciliation sweeper and the driver orchestration both key off.

### `driver_evaluation.status` (the join, one per driver batch)

```
WAITING → READY → EVALUATING → DECIDED → POSTED
```

`WAITING → STALE` when no progress past SLA (found by the sweeper) → `READY` when the sweeper
re-signals. `READY` also re-verifies against Mercury before proceeding to `EVALUATING`. See
[Driver Orchestration](stages/06-driver-orchestration.md).

Full diagram: `docs/architecture/Figure3_Status_Lifecycle.png`.

## Indexes that matter

```sql
-- idempotent ingest
CREATE UNIQUE INDEX ON dmer_document (document_guid);
-- the sweeper's main scan
CREATE INDEX ON dmer_document (pipeline_status, updated_at);
-- driver completeness checks
CREATE INDEX ON dmer_document (driver_key, pipeline_status);
CREATE INDEX ON driver_evaluation (status, last_mercury_check_at);
-- outbox publisher poll
CREATE INDEX ON mercury_outbox (status, next_attempt_at) WHERE status = 'PENDING';
-- duplicate detection
CREATE INDEX ON dmer_extraction (comparison_hash);
-- stage timings and replay
CREATE INDEX ON dmer_stage_run (document_id, stage, attempt_no DESC);
```

## `document_guid` is not a content key

`dmer_document.document_guid` is unique so that the same **delivery** (poller re-scan, webhook
retry, redelivered message) is a no-op. Per question M-7, Mercury confirmed `document_guid` is
**not** guaranteed stable across a re-upload of the same physical document — a re-uploaded PDF may
get a new guid while belonging to the same driver and case. This means:

- `document_guid` uniqueness prevents re-processing the *same delivery* twice. It does **not**
  detect duplicate *content* — that is `dmer_extraction.comparison_hash`'s job, evaluated at the
  driver join (see [Decision Gateway](stages/07-decision-gateway.md), step 3).
- Do not add logic anywhere that treats "new `document_guid`" as "definitely a new document" for
  business purposes — only for delivery idempotency.

## Alignment gaps vs. current code

`libs/dmer_common/src/dmer_common/db/documents.py` and `status.py` implement a `documents` table
and a `DocumentStatus` enum (`received → extracting → sectioning → combining → combined →
published`, plus `failed`) built for the original architecture's single `di-processor`
responsibility. Under the revised architecture:

- The table is `dmer_document`, with the column set above — not `documents`.
- Status is **two fields** (`current_stage`, `pipeline_status`), not one five-value enum scoped to
  extraction sub-steps.
- Ten more tables (`dmer_stage_run`, `driver`, `driver_evaluation`, `dmer_extraction`,
  `rule_evaluation`, `rules_version`, `dmer_decision`, `mercury_outbox`, `processing_error`,
  `poll_checkpoint`) have no repository code yet.

**Recommendation:** replace `documents.py`/`status.py` with a `dmer_document.py` repository (same
`ON CONFLICT` idempotency pattern is reusable) plus a repository per table above, and add a shared
`dmer_stage_run` writer (start/succeed/fail helper) that every stage's activity/function calls —
see each stage doc's "Implementation considerations" section. The transition-validation approach in
`status.py` (`is_valid_transition`, unit-testable without a database) is worth keeping as a pattern
for the new `pipeline_status` and `driver_evaluation.status` state machines.

## Open Questions / Decisions Required

- **`driver_evaluation (driver_key, open) DO UPDATE`** — the architecture document specifies this
  conflict target but does not define what "open" means (a boolean column? a partial unique index
  on `status NOT IN ('DECIDED', 'POSTED')`?). Needs a decision before the migration is written.
  Related to question I-10 (a new DMER after a batch is posted is "treated separately" — implying a
  *new* `driver_evaluation` row per batch, not a reopened one).
- **Outcome code definitions** (`CP`, `IN`, `PR`, `PU`, `PCM`, `CR`) are referenced throughout the
  architecture document but never defined there. Source the business definitions from Intake before
  building the rule engine's outcome table and `dmer_decision.outcome_code` enum.
- **Normalized clinical JSON in Postgres** (question I-17 answered "30–90 days" for blob retention,
  but whether the *normalized* JSON may also live as `jsonb` on `dmer_extraction` — for reporting
  and rule tuning — needs separate privacy/security sign-off per §9.2 and §7.3 of the architecture
  document). Until confirmed, do not add a normalized-content jsonb column.
