# Stage 3 — Document Orchestration

Source: architecture doc §4.3. Figure: `docs/architecture/Figure1_Revised_Architecture.png`
("Stage 3 — Document Orchestration, Durable Function, one instance per document"). Data model:
`../data-model.md`. Messages: `../message-contracts.md`. Activities:
[Resolve Driver](#activity-resolve-driver) (below), [Normalize](04-activity-normalize.md),
[Rule Engine](05-activity-rule-engine.md).

## Purpose

A Durable Functions orchestration with `instanceId` set to `document_guid`, started from the
`dmer-extracted` queue. It runs three activities in sequence (Resolve Driver, Normalize, Rule
Engine) and then signals the driver. Using an
orchestration (rather than two more queue-triggered functions) buys automatic per-activity retry
semantics, a durable record of where the document reached, and the ability to add steps later
without adding queues.

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable Functions orchestrator, started by a `dmer-extracted` queue trigger |
| Reads | Message: `{ document_id, driver_key, extracted_blob_url }` (`driver_key` may be null) |
| Runs | `Activity: Resolve Driver` → `Activity: Normalize` → `Activity: Rule Engine`, in sequence |
| Publishes | `driver-decision`, with `SessionId = driver_key`, at the end |

## How Durable Functions actually move work

Worth stating precisely — it determines how the normalizer is invoked and why it does not need to
be a separate service, and it is where developers new to Durable Functions most often go wrong.

The orchestrator does **not** send Service Bus messages to its steps. It calls
`CallActivityAsync("NormalizeDmer", input)`. Underneath, the Durable extension writes that
instruction to its own control queues in the task hub storage account and **replays the
orchestrator function from the beginning** each time an activity completes, using the event history
table to skip work already done. Three consequences follow:

1. **Orchestrator code must be deterministic.** No HTTP calls, no database access, no
   `DateTime.Now`, no `Guid.NewGuid()`, no random numbers, no environment reads. Use
   `context.CurrentUtcDateTime` and `context.NewGuid()`. Every piece of input/output lives in an
   activity. A non-deterministic orchestrator does not fail loudly — it produces different results
   on replay and corrupts its own history.
2. **Activity inputs/outputs are serialized through storage.** Pass `document_id`, `driver_key`,
   and blob URLs. **Never** pass extracted or normalized JSON as an activity argument or return
   value — it inflates the history table and slows every subsequent replay.
3. **Long work has three patterns:**
   - A call of up to a few minutes → a plain activity.
   - Longer work → either the async HTTP pattern (activity starts the job, orchestrator polls
     between `context.CreateTimer` waits) or an external event (`RaiseEventAsync` +
     `WaitForExternalEvent` with a timeout).
   - For an Azure OpenAI call of 30–120 seconds (Normalize), **a plain activity is correct** — the
     other two patterns are unnecessary complexity.

## Orchestration flow

1. Receive `{ document_id, driver_key, extracted_blob_url }` from `dmer-extracted`.
2. `CallActivityAsync("ResolveDriver", ...)` — see [Activity: Resolve Driver](#activity-resolve-driver).
   Returns the resolved `driver_key`, or routes the document to `MANUAL_REVIEW` (the orchestration
   then ends without publishing). **Must run first**: the Rule Engine activity increments the
   driver's `driver_evaluation.completed_document_count`, so the driver and its evaluation must
   exist before it runs.
3. `CallActivityAsync("NormalizeDmer", ...)` — see [Activity: Normalize](04-activity-normalize.md).
4. `CallActivityAsync("RunRuleEngine", ...)` — see [Activity: Rule Engine](05-activity-rule-engine.md).
5. Publish to `driver-decision` with `SessionId = driver_key` (the one resolved in step 2) — this
   is the hand-off to [Driver Orchestration](06-driver-orchestration.md); the per-document
   orchestration's job ends here.

Two conventions apply to every activity in this orchestration (and to every stage in the pipeline)
and are not repeated per-activity: each activity writes its own `dmer_stage_run` row
(`RUNNING` → `SUCCEEDED`/`FAILED`), and each activity updates `dmer_document.current_stage` /
`pipeline_status` on success. See `../data-model.md`.

## Activity: Resolve Driver

**Decided (2026-09-23):** driver identification and document counting run here, not in
[Extraction](02-extraction.md#driver-resolution-is-not-performed-in-extraction), so they complete
before `driver-decision` (which requires `driver_key`) is published.

| | |
|---|---|
| Type | Durable activity, first in the orchestration |
| Reads | `dmer_document.driver_key` (from Ingest, when Mercury supplied a driver), `dmer_extraction.licence_number_read` (canonical 8-digit, from Extraction), Mercury `GET by driver_licence` |
| Writes | `driver`, `driver_evaluation`, `dmer_document.driver_key` |
| Returns | The resolved `driver_key` (never the licence number, per §9.2) |

Steps:

1. **Mercury supplied a driver** (`driver_key` already set by Ingest): if `licence_number_read` is
   present and differs from `driver.licence_number`, record the discrepancy; keep Mercury's driver.
2. **Mercury supplied none**: use `licence_number_read`.
   - Null (unreadable, invalid, or the top of the page cut off) → route to `MANUAL_REVIEW` (I-12).
   - Look it up with Mercury `GET by driver_licence`. Exactly one driver → upsert `driver` on
     `licence_number` (`INSERT ... ON CONFLICT (licence_number) DO UPDATE`) and attach its
     `driver_key` to `dmer_document`. Flag the document so [Decision Gateway](07-decision-gateway.md)
     step 6 records `proposed_driver_key` / `MAP_DRIVER` (I-11: the AI may map automatically).
   - No match, or more than one → route to `MANUAL_REVIEW`, carrying the licence information and
     reason for the Mercury comment (I-12).
3. **Create or attach the driver's open `driver_evaluation`** and set `expected_document_count`
   from the same Mercury `GET by driver_licence` response (`status = WAITING`,
   `last_mercury_check_at`). Applies to both branches above.

Licences are compared in the canonical form from `dmer_common.licence.normalize_licence`, the same
one Ingest uses for `driver.licence_number`. Never log the licence or put it in an activity return
value or queue message; use `driver_key`.

Idempotency: an activity retry must not create a second driver or evaluation, or double-count.
`driver` upserts on `licence_number`; `driver_evaluation` upserts on its open-evaluation conflict
target; `expected_document_count` is **set** from Mercury, not incremented, so a retry rewrites the
same value.

**Blocked on:** the meaning of "open" for the `driver_evaluation (driver_key, open)` conflict target
(`../data-model.md#open-questions--decisions-required`), and the Mercury `GET by driver_licence`
API contract (response shape, auth); `libs/dmer_common/src/dmer_common/mercury_client/` is a stub.

## Failure handling

Durable Functions' built-in activity retry policy (`RetryOptions`) covers transient failures inside
an activity without custom code. Distinguish, same as every other stage:

- Transient (429s, timeouts) → activity-level retry with backoff.
- Poison (malformed normalized JSON, schema validation failure with no retry path) → the activity
  raises a terminal exception; the orchestrator catches it and routes the document to
  `MANUAL_REVIEW` (`pipeline_status`) rather than letting the orchestration instance fail silently.
  This is what "retries exhausted → MANUAL_REVIEW" means on
  `docs/architecture/Figure3_Status_Lifecycle.png`.
- Driver not resolvable (Resolve Driver: no readable licence, no match, or ambiguous match) → the
  activity routes the document to `MANUAL_REVIEW` (I-12) and the orchestrator ends **without**
  publishing to `driver-decision`; a document with no `driver_key` can never join a driver batch.
- Mercury `GET by driver_licence` failure → transient; activity-level retry with backoff. Never
  guess a driver on retry exhaustion; route to `MANUAL_REVIEW`.

## Interaction with upstream/downstream stages

Started by `dmer-extracted` (from [Extraction](02-extraction.md)). Publishes to `driver-decision`,
consumed by [Driver Orchestration](06-driver-orchestration.md). This is the last per-document,
fully-parallel stage — everything after this point is serialized per driver.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `AzureWebJobsStorage` / task hub storage account | Durable Functions history/control-queue storage — separate from `dmer_common`'s own storage client. |
| `SERVICEBUS_NAMESPACE`, `DMER_EXTRACTED_QUEUE`, `DRIVER_DECISION_QUEUE` | See `../message-contracts.md`. |
| `MERCURY_DRIVER_LICENCE_API_BASE_URL` + Mercury credentials | Resolve Driver's `GET by driver_licence` call; shared with [Decision Gateway](07-decision-gateway.md). |

## Authentication / identity

Managed identity for the task hub storage account, Service Bus, PostgreSQL. Mercury credentials
(Key Vault) for the Resolve Driver activity. No direct external calls from the orchestrator itself
— those live in the activities.

## Idempotency requirements

Durable Functions' replay model gives idempotency **within** an orchestration instance for free
(an activity that already succeeded is not re-run on replay). The remaining requirement is at the
boundary: the `dmer-extracted` trigger must not start a second orchestration instance for the same
`document_guid` if the message is redelivered — set `instanceId = document_guid` explicitly when
starting the orchestration; the Durable extension rejects a duplicate `instanceId` for an
already-running or completed instance (configurable via `overridable_existing_instances`).

## Logging / auditing

`document_id` flows from the queue message through both activities (bind it once at the top of
the orchestrator's activity-input construction — but remember step 1's determinism rule: the bind
itself must not do I/O). `dmer_stage_run` rows from both activities are the audit trail; no
additional orchestrator-level audit table is needed.

## Implementation considerations for Claude Code

- `services/workflow-orchestrator/` is the placeholder folder for this stage (currently an empty
  `FunctionApp()`) — this is the natural home for the Document Orchestrator, and (per the
  architecture) for the Normalize and Rule Engine activities as well, since both now run
  **in-process** rather than as separate deployables. See
  [Alignment gaps](#alignment-gaps-vs-current-code).
- The `durable-functions` Python package is not yet in any `requirements.txt` (all are placeholder
  stubs — see `services/workflow-orchestrator/requirements.txt`); add it here.
- No orchestrator/activity code exists yet to reuse or rework — this is greenfield within the
  existing placeholder file.

## Alignment gaps vs. current code

`docs/services/workflow-orchestrator.md` (old) describes a **single** orchestration covering
metadata loading, duplicate validation, case creation, normalization, rule evaluation, Mercury
update, and completion marking — i.e., everything through Mercury write-back in one instance. The
revised architecture **splits** this into two orchestrations:

- **Document Orchestration** (this doc): Normalize + Rule Engine only, per document, ends by
  signalling the driver.
- **Driver Orchestration** ([06](06-driver-orchestration.md)): Decision Gateway + Post-Processing,
  per driver, serialized.

Duplicate validation and Mercury write-back moved to the Driver Orchestration, because they require
seeing every sibling document for the driver — a single per-document orchestration cannot do this
safely (see [Driver Orchestration §Why serialization is required](06-driver-orchestration.md)).
`services/workflow-orchestrator/` should be read as covering *this* stage only going forward; the
driver-level work needs its own service (see `06-driver-orchestration.md`'s implementation notes
for the recommended folder).

## Open Questions / Decisions Required

- **`driver_evaluation (driver_key, open)`**: what "open" means (blocks Resolve Driver step 3).
- **Mercury `GET by driver_licence` contract**: response shape and auth (blocks Resolve Driver).
- **Discrepancy recording**: where a Mercury-driver vs page-licence mismatch is stored (a flag on
  `dmer_extraction`, or a `processing_error` row); no column is defined yet.
- Whether Normalize and Rule Engine activity code lives as Python functions directly inside
  `services/workflow-orchestrator/`, or as importable modules in `libs/dmer_common` invoked from
  there — the architecture document specifies them as "a Durable Functions activity" and "a library
  call inside a Durable activity" respectively, but not the module boundary. Recommendation: keep
  activity *triggers* thin in `workflow-orchestrator`, put the normalization-schema logic and the
  rule-engine wrapper in `libs/dmer_common` (or a new `libs/dmer_rules` if the rule library gets
  large) so both are unit-testable without a Functions host.
