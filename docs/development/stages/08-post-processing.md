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
the four operation types (`UPDATE_OUTCOME`, `MARK_DUPLICATE`, `MAP_DRIVER`, `CREATE_CASE`). A single
document can need more than one outbox row (e.g. `UPDATE_OUTCOME` + `MARK_DUPLICATE` for a
duplicate; `UPDATE_OUTCOME` + `MAP_DRIVER` when a proposed mapping exists).

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

## Example payload — `mercury_outbox.payload` for `UPDATE_OUTCOME`

```json
{
  "document_guid": "123e4567-e89b-...",
  "outcome_code": "IN",
  "comment": "AI could not process — extraction retries exhausted",
  "duplicate_of": null
}
```

(Illustrative — the exact Mercury POST/PUT payload shape is question M-6, not yet confirmed by the
Mercury team; see [Open Questions](#open-questions--decisions-required).)

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
