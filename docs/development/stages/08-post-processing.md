# Activity: Post-Processing

Source: architecture doc §4.5. Part of [Driver Orchestration](06-driver-orchestration.md).
Data model: `../data-model.md`. Messages: `../message-contracts.md`.

## Purpose

Writes the decision rows and the outbox rows **in one database transaction** and finishes. Does
**not** call Mercury — a separate function ([Outbox Publisher](09-reliability-components.md#outbox-publisher))
drains the outbox.

## Why post-processing never calls Mercury directly

If post-processing called Mercury directly, a failure after the decision was written but before
Mercury acknowledged would leave a decision that nobody knows was never delivered — precisely the
invisible-DMER failure this whole design exists to prevent. Committing the decision and the intent
to publish it together makes that state unreachable. This is the [outbox
pattern](../services/azure-database-postgresql.md#outbox-pattern); see also
[Reliability Components](09-reliability-components.md).

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable activity inside the Driver Orchestrator |
| Reads | The Decision Gateway's computed outcome for every document in the batch |
| Writes | `dmer_decision` and `mercury_outbox`, in a single transaction; `dmer_document`; `driver_evaluation` |
| Calls Mercury | No — the Outbox Publisher does that |

## Database writes — the transaction

```sql
BEGIN;
  INSERT INTO dmer_decision  (...) VALUES (...);   -- one per document in the batch
  INSERT INTO mercury_outbox (document_id, operation, payload, idempotency_key, status)
       VALUES (:doc, 'UPDATE_OUTCOME', :payload, :key, 'PENDING');
  UPDATE driver_evaluation SET status = 'DECIDED', evaluated_at = now() WHERE id = :eval;
COMMIT;
```

Because both statements are in one transaction, a crash either leaves nothing or leaves both.
There is no state in which a decision exists but no one is trying to deliver it.

| Table | Operation | Fields |
|---|---|---|
| `dmer_decision` | `INSERT`, one row per document in the batch, inside the transaction | `id`, `document_id`, `driver_evaluation_id`, `outcome_code`, `is_duplicate`, `duplicate_of_document_id`, `duplicate_reason`, `superseded_by_cutoff_rule`, `driver_mapped`, `proposed_driver_key`, `decision_reason` (jsonb — the rule path, the diff that forced `IN`, the cut-off flags that applied), `decided_by = AI`, `decided_at`. |
| `mercury_outbox` | `INSERT`, one row per intended Mercury operation, inside the same transaction | `id`, `document_id`, `operation`, `payload` (jsonb), `idempotency_key`, `status = PENDING`, `attempt_count = 0`, `next_attempt_at = now()`. |
| `dmer_document` | `UPDATE` | `pipeline_status = POSTING`, `current_stage = POST`, `updated_at`. |
| `driver_evaluation` | `UPDATE` | `status = DECIDED`, `evaluated_at`. |

One `mercury_outbox` row is produced per intended operation per document — see
`../message-contracts.md#mercury_outbox-operations-not-a-queue-message--a-db-driven-payload` for
the operation types (`UPDATE_OUTCOME`, `MAP_DRIVER`, `CREATE_CASE`; `MARK_DUPLICATE` is no longer
queued — a duplicate is flagged on its `UPDATE_OUTCOME`). A single document can need more than one
outbox row (e.g. `UPDATE_OUTCOME` + `MAP_DRIVER` when a proposed mapping exists).

### Case creation rule (question I-13, answered)

If there is no open case for the driver, `CREATE_CASE`; otherwise attach to the existing open case.
If a case is already open for the driver, it might belong to a different DMER for the same driver —
check for duplicate/cut-off scenarios and update based on the outcome. **In all cases, the reason
must be mentioned in the Mercury comment** (this applies to every outbox operation Post-Processing
queues, not only `CREATE_CASE`).

### Fallback status wording (question I-1, answered)

When the pipeline cannot produce an outcome at all, default to `IN`, **and add "AI could not
process" as a comment** — this comment is written back to Mercury as part of the POST/PUT payload,
not just logged internally. This applies to the fallback path in
[DLQ Drain](10-dlq-drain.md#database-writes) and to any decision written with `decided_by = FALLBACK`.

### Decision immutability after human review (question I-2, answered)

Once an AI outcome is posted and pushed to the AI-processed queue, and a human has since touched
it, **the AI does not revise it.** A human review updates the record directly in Mercury; that
update does not need to flow back into this system. Implication: Post-Processing/the Decision
Gateway must never overwrite a `dmer_decision` row for a document that has already been posted and
human-reviewed — if a later sibling document changes the duplicate picture for an already-decided
document, treat it as the [I-10](07-decision-gateway.md) "new DMER, evaluate separately" case, not
as a revision of the prior decision.

## Failure handling

The transaction itself is the failure-handling mechanism — see
[Reliability Components §Outbox pattern](09-reliability-components.md#outbox-pattern) for what
happens after commit (the Outbox Publisher's retry/backoff) and
[§Reconciliation Sweeper](09-reliability-components.md#reconciliation-sweeper) for what happens if
the whole activity never runs (a stuck `driver_evaluation`).

## Interaction with upstream/downstream

Reads the Decision Gateway's output within the same orchestration instance. Writes rows that the
[Outbox Publisher](09-reliability-components.md#outbox-publisher) later drains to Mercury.

## Idempotency requirements

`mercury_outbox.idempotency_key` (unique) is what makes the *delivery* safe to retry — see
[Outbox Publisher](09-reliability-components.md#outbox-publisher). The *write* of this transaction
itself is protected by the same driver-serialization guarantee as the rest of
[Driver Orchestration](06-driver-orchestration.md) — it should only ever run once per driver batch.

## Logging / auditing

Log one line per outbox row created (`document_id`, `operation`, `idempotency_key`) — this is the
line an incident review greps for when asked "did we even try to post this."

## Mercury write-back — `setDmerAIProccessedStatus` (confirmed 2026-11-20; path per Mercury DEV's Swagger, 2026-11-24)

`UPDATE_OUTCOME` is sent as
`POST {MERCURY_WRITE_API_BASE_URL}/Dmers/{document_guid}/setDmerAIProccessedStatus` — the DMER
in the path, not the body (`dmer_common.mercury_client.ai_status`); `aiProcessedStatus` is the
only required field:

```json
{
  "aiProcessedStatus": 100000002,
  "aiDpsPriority": 100000000,
  "aiQueue": 100000000,
  "aiDmerStatus": 100000000,
  "aiIsDuplicate": true,
  "notes": "<the comment>"
}
```

| Case | `aiProcessedStatus` | `aiDpsPriority` | `aiQueue` | `aiDmerStatus` | Other |
|---|---|---|---|---|---|
| Outcome `CP` | Proccessed | Regular | Team - Intake | Manual Pass | `notes` = comment |
| Outcome `IN` | Proccessed | Regular | Team - Intake | Adjudicate | `notes` = comment |
| Outcome `PR` | Proccessed | Regular | Team - Adjudicators | Adjudicate | `notes` = comment |
| Outcome `PU` | Proccessed | Urgent/ Immediate | Team - Adjudicators | Adjudicate | `notes` = comment |
| Outcome `TCM` | Proccessed | Regular | Team - Case Managers | Adjudicate | `notes` = comment |
| Outcome `CR` | Proccessed | Critical Review | Team - Adjudicators | Adjudicate | `notes` = comment |
| Duplicate (I-7) | Proccessed | — | — | — | `aiIsDuplicate: true`, `notes` = "Duplicate of DMER <guid>." |
| Processing error (any fallback, `decided_by = FALLBACK`) | **Error** | Regular | Team - Intake | Adjudicate | `notes` = "Unable to AI process due to error. <details>" |

Option-set values: `aiProcessedStatus` NA 100000000, InProgress 100000001, Proccessed 100000002,
Error 100000003; `aiDmerStatus` Adjudicate 100000000, Reject 100000001, Clean Pass 100000002,
Manual Pass 100000003; `aiQueue` Team - Intake 100000000, Team - Adjudicators 100000001,
Team - Case Managers 100000002, Team - DIP Team Lead 100000003, Team - Remedial Intake 100000004,
Team - DIP Adjudicators 100000005; `aiDpsPriority` Regular 100000000, Urgent/ Immediate 100000001,
Expedited 100000002, Critical Review 100000003.

The outbox row itself (`mercury_outbox.payload` for `UPDATE_OUTCOME`) keeps the pipeline's
fields — `document_guid`, `outcome_code`, `decided_by`, `comment`, and for a duplicate
`is_duplicate` + `duplicate_of_document_guid` — and the request is built from it at send time.

### Driver mapping — `add_driver` (confirmed 2026-11-20)

`MAP_DRIVER` (I-11: a driver matched from the licence read on the page) is sent as
`POST {MERCURY_WRITE_API_BASE_URL}/dmers/{documentRecordId}/add_driver`
(`dmer_common.mercury_client.driver_link`):

```json
{
  "driverLicenceNumber": "<licence>",
  "firstName": "<Mercury's first name>",
  "surname": "<Mercury's last name>",
  "comments": "Driver linked by the AI from the licence number read on the DMER.",
  "dmerId": "<documentRecordId>"
}
```

The licence and names are the matched Mercury driver's (`driver` table), carried on the
`MAP_DRIVER` outbox row (PII: database only, never logs); `dateOfBirth` is not sent. Mercury links
the driver (or creates one if it doesn't exist — the pipeline only sends drivers Mercury already
holds) and answers `{"resultMessage", "driverOpenCase": {"caseId", "caseTitle", "caseType",
"dmerStatus"}}`, kept as the row's `mercury_response`. Mercury's error shape
`{"errorStatus": 1, "replyMessage", "caseId"}` — on this or any write, even with a 2xx — fails the
row permanently, with the reply message on `last_error`.

**If `add_driver` fails for good** (rejected, or out of retries), the outcome is still sent, as a
processing error for manual review: `aiProcessedStatus` Error, IN routing (Regular, Team - Intake,
Adjudicate), `notes` = "Unable to AI process due to error: the driver could not be linked to the
DMER (‹error›). Driver details as read from the DMER form: licence ‹licence›, first name ‹first name›, surname ‹surname›. ‹the AI's comment›". The DMER's `CREATE_CASE` is blocked; the failed rows
stay `FAILED` for a person to resolve. The driver details are what the form shows, read from the
extraction's `combined.json` (`dl_number`, `first_name`, `last_name`; `dmer_common.form_driver`)
once, when `add_driver` fails, and saved on the outcome row as `form_driver_note`. Parts the form
doesn't give are left out; if `combined.json` can't be read, the note goes without them. The
reliability app reads `extracted-dmer` with a container-scoped Storage Blob Data Reader role
(`BLOB_ACCOUNT_URL`).

### Case — `add_case` (confirmed 2026-11-20)

`CREATE_CASE` (I-13) is decided when it is sent (`MercuryClient._ensure_case`,
`dmer_common.mercury_client.case_link`):

0. **The AI linked the driver** (`MAP_DRIVER` was sent — e.g. a DMER on Mercury's placeholder
   driver `00000000`, decided 2026-11-23): the publisher saves `add_driver`'s `driverOpenCase` on
   the `CREATE_CASE` row (`driver_linked`, `driver_open_case`). A real active case → nothing
   sent; no active case, or the placeholder driver's case (title starting with
   `00000000` — not expected from `add_driver` for a real licence, to confirm with Mercury) →
   `add_case`. The DMER's own (placeholder) case and the live lookup are not consulted.
1. The DMER arrived **with a case** (`dmer_document.mercury_case_id`, from the batch's
   `dmerDocumentCaseRecord`, carried on the row) → nothing is sent (`skipped: dmer_has_case`).
2. Otherwise a live `POST drivers/lookup` by licence: the **driver has an open case** → nothing is
   sent (`skipped: driver_has_open_case`); Intake handles it. The batch API and the lookup return
   only active cases (2026-11-20, to confirm with Mercury), so any `case` present counts as open;
   one whose status reads closed/cancelled/inactive/complete, should Mercury ever send it, doesn't.
3. Otherwise `POST {MERCURY_WRITE_API_BASE_URL}/dmers/{documentRecordId}/add_case`:

```json
{
  "case_type": "DMER",
  "owner": "TBD",
  "driver": { "driver_id": "<licence number>" },
  "document_type": "DMER",
  "comments": "Case created by the AI: the DMER needed a case and the driver had no active one."
}
```

**snake_case**, with `case_type`, `document_type` and `driver.driver_id` required. Mercury DEV
binds this body in snake_case although its Swagger shows camelCase (`caseType`,
`driver.driverId`, `documentType`): a camelCase body is answered 422 "The CaseType field is
required" (confirmed 2026-11-26). Mercury answers 201 `{"caseId", "documentRecordId", "status":
"Created"}` and attaches the comment to the driver and the case; 404 when the driver doesn't
exist, 409 when the DMER belongs to a different driver or already has a case, 422 when a
required field is missing (errors as `ProblemDetails`). `case_type`/`owner`/`document_type` are
Mercury's sample values (owner "TBD" to confirm). The driver's open case is read from the
lookup's `activeCase`.

**If `add_case` fails for good**, the outcome still goes **as usual** (its own status, priority
and queue), with "The case could not be created for this DMER (‹error›)." added to its notes;
the `CREATE_CASE` row stays `FAILED` and the document `POSTING`, for a person to resolve (unlike a
failed driver link, it isn't moved to `MANUAL_REVIEW`).

## Implementation

Built into the Decision Gateway's own transactions rather than a separate activity -- the
gateway already writes `dmer_decision` there, so the outbox rows join the same transaction:

- `dmer_common.db.mercury_outbox` -- the table, the pure row builders (`decision_rows`,
  `fallback_rows`, the comment wording) and `queue_outbox_rows` (insert in the caller's
  transaction, `ON CONFLICT (idempotency_key) DO NOTHING`, one log line per row).
- `DriverDecisionRepository.record_decided` -- per AI decision: `MAP_DRIVER` when a driver was
  matched from the licence (I-11), `CREATE_CASE` always (the publisher sends `add_case` only when
  the DMER has no case and the driver no open case, I-13), `UPDATE_OUTCOME` always — flagged `is_duplicate` for a rejected
  clean-pass copy (I-7); documents -> `POSTING`.
- `DriverDecisionRepository.route_batch_to_manual_review` (Mercury unverifiable) -- an
  `UPDATE_OUTCOME` IN per document, "Unable to AI process due to error" plus the note (I-1).
- `DriverLookupRepository.manual_review` (I-12/I-22: no driver file found or mapped) -- an
  `UPDATE_OUTCOME` IN with "Unable to AI process due to error", that the driver's file could not be found or
  mapped, the reason and the licence read. There is no driver evaluation, so no `dmer_decision`
  row; the outbox row is the outcome. Its key, `<document_id>:FALLBACK:UPDATE_OUTCOME`, is the
  one the DLQ Drain's fallback will use, so a document can't get two fallbacks.

Idempotency keys are `<dmer_decision.id>:<operation>`. The publisher applies a document's rows in
`OPERATION_ORDER` (driver, case, outcome). The comment always states the outcome and the reason
(I-13).

## Implementation considerations for Claude Code

- No placeholder code exists for this activity — `services/post-processing/` currently models the
  **original architecture's** version of this component (a topic-triggered Azure Function; see
  [Alignment gaps](#alignment-gaps-vs-current-code)) and needs to be repurposed as a Durable
  activity inside the Driver Orchestration, not a standalone topic subscriber.
- The single-transaction requirement means this activity's DB access should use one connection/
  transaction scope for all three writes — do not reuse three separate repository calls that each
  open their own transaction.

## Alignment gaps vs. current code

`docs/services/post-processing.md` (old) describes post-processing as an **Azure Function
subscribed to the `dmer-lifecycle-events` topic** (`sub-post-processing`), writing an `audit_log`
table, emitting custom metrics, and sending notifications — a fire-and-forget consumer of a
completion event, not the component that commits the decision itself. Under the revised
architecture, Post-Processing is a **Durable activity inside the Driver Orchestration** that writes
`dmer_decision` + `mercury_outbox` transactionally; there is no `dmer-lifecycle-events` topic and no
`audit_log` table in the revised schema (`dmer_stage_run` is the audit trail instead — see
`../data-model.md`). `services/post-processing/`'s placeholder `function_app.py` has no working
code to preserve, so retiring the topic-subscriber model is safe, but the folder's *role* changes
significantly enough that it should be explicitly repurposed (moved to live alongside/inside the
new `services/driver-orchestrator/` folder proposed in
`06-driver-orchestration.md#alignment-gaps-vs-current-code`) rather than kept as an independent
Function App. See the top-level `../README.md#open-questions--decisions-required`.

## Open Questions / Decisions Required

- **M-6** — do Mercury's POST/PUT endpoints accept an idempotency key, and what happens on a
  duplicate submit? Directly determines whether `mercury_outbox.idempotency_key` actually protects
  against a double-post at Mercury's end, or only prevents the Outbox Publisher from *trying* twice
  on our side.
- **M-10** — expected concurrent load and any throttling on Mercury's write APIs — not yet
  confirmed; sets the Outbox Publisher's batch size and concurrency (see
  [Reliability Components](09-reliability-components.md)).
- Exact Mercury POST/PUT payload shape — Mercury team has not yet shared the structure (M-6 follow-up).
