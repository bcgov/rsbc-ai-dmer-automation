# Stage 4 — Driver Orchestration (the join)

Source: architecture doc §4.4. Figure: `docs/architecture/Figure1_Revised_Architecture.png`
("Stage 4 — Driver Orchestration, the join — serialized per driver"). Data model: `../data-model.md`.
Messages: `../message-contracts.md`. Activities: [Decision Gateway](07-decision-gateway.md),
[Post-Processing](08-post-processing.md).

**This is the most important change from the original architecture, and the part most likely to be
built incorrectly if treated as ordinary per-document work.** Read this whole document before
touching this stage's code.

## Purpose

Everything up to the rule engine is per-document and embarrassingly parallel. The decision gateway
is not: it is a **join across all documents belonging to one driver**. If each document's
orchestration independently asked "are the rest of this driver's documents finished?", three
failure modes follow (see below). The driver orchestration exists to make that question ask-once,
serialized, and its answer a durable row rather than an in-memory branch.

## Why serialization is required

| Failure | How it happens |
|---|---|
| **Lost update** | Document A and document B finish within the same second. Neither has committed its own completion when the other reads, so both conclude the driver is incomplete and both stand down. The driver is never evaluated and no outcome is ever posted. This is the silent-orphan case — the failure that makes a DMER disappear from both the AI queue and the manual queue. |
| **Double posting** | The opposite timing: both see the set as complete, both run duplicate detection, both write decisions, both post to Mercury — potentially with different results depending on what each one read. |
| **No owner** | "If not all documents are processed, do nothing" is a terminal state with no row recording it. Nothing can find it, report it, or retry it. |

## The mechanism

- **Two orchestrations.** The per-document orchestration ([Document Orchestration](03-document-orchestration.md))
  ends by signalling the driver. A **separate** per-driver orchestration, with `instanceId = driver_key`,
  runs the Decision Gateway and Post-Processing.
- **One at a time per driver.** Use a Service Bus **session** with `SessionId = driver_key` on the
  `driver-decision` queue, or a Durable Entity keyed on `driver_key`. Either gives single-writer
  semantics for free. A Postgres advisory lock (`pg_advisory_xact_lock(hashtext(licence))`) is a
  valid third option if the team prefers coordination to live in the database. **Pick one and
  document it** — mixing approaches gives the illusion of safety without the guarantee. This
  documentation set assumes the Service Bus session approach (matches the queue config in
  `../message-contracts.md`) unless the team decides otherwise — flagged as an
  [open question](#open-questions--decisions-required) because the architecture document
  deliberately leaves the choice open.
- **The wait is a row, not a branch.** `driver_evaluation` holds `status`, `expected_document_count`,
  `completed_document_count`. `WAITING` is a state the [Reconciliation Sweeper](09-reliability-components.md)
  can query. A code path that silently returns is not.
- **Verify twice.** `expected_document_count` is set from the Mercury `GET by driver_licence`
  call by [Document Orchestration's Resolve Driver activity](03-document-orchestration.md#activity-resolve-driver)
  (not Extraction). It is **re-verified against a fresh call at decision time**, because
  a new document may have arrived in between and the batch may no longer be what was assumed.

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable Functions orchestrator, `instanceId = driver_key`, started by a session-enabled `driver-decision` queue trigger |
| Runs | `Activity: Decision Gateway` → `Activity: Post-Processing` |
| Guarantees | Exactly one evaluation at a time for a given driver (a Durable Entity keyed on `driver_key` works too — see above) |

## Orchestration flow

1. Receive a driver-ready signal on `driver-decision` (session = `driver_key`).
2. `CallActivityAsync("RunDecisionGateway", ...)` — see [Decision Gateway](07-decision-gateway.md).
   If the gateway determines the driver is not yet complete, it sets `driver_evaluation.status =
   WAITING` and the orchestration **ends** (not fails) — the
   [Reconciliation Sweeper](09-reliability-components.md) is what re-signals it later.
3. If complete, `CallActivityAsync("RunPostProcessing", ...)` — see [Post-Processing](08-post-processing.md).
4. Orchestration instance completes.

## Interaction with upstream/downstream stages

Started by `driver-decision` (from every sibling document's [Document Orchestration](03-document-orchestration.md)
instance — the last document to finish is whichever signal actually triggers the gateway to see a
complete set). Post-Processing writes to `mercury_outbox`, drained by the
[Outbox Publisher](09-reliability-components.md#outbox-publisher). Re-signalled by the
[Reconciliation Sweeper](09-reliability-components.md#reconciliation-sweeper) when a `WAITING`
row is found to actually be complete.

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `SERVICEBUS_NAMESPACE`, `DRIVER_DECISION_QUEUE` | Session-enabled — see `../message-contracts.md`. |
| `MERCURY_DRIVER_LICENCE_API_BASE_URL` | The `GET by driver_licence` completeness call — see [Decision Gateway](07-decision-gateway.md). |
| `DRIVER_JOIN_LOCK_STRATEGY` | Not yet decided — Service Bus session vs. Durable Entity vs. Postgres advisory lock; see [Open Questions](#open-questions--decisions-required). |

## Authentication / identity

Managed identity for Service Bus (session receive), PostgreSQL, and the Mercury call (via
`mercury_client`, Key Vault-backed credentials over ExpressRoute).

## Idempotency requirements

The orchestration's `instanceId = driver_key` prevents two concurrent instances for the same
driver from being *started* twice by the Durable extension. The serialization guarantee above (one
at a time) is what prevents the lost-update/double-posting races **within** a logically "new" run
triggered by a fresh signal after the previous instance completed — these are two different
protections and both are required.

## Logging / auditing

Log every transition of `driver_evaluation.status` with `driver_key` and the triggering signal
(document completion vs. sweeper re-signal) — this is the trail that answers "why did this driver
wait N hours" during an incident review.

## Implementation considerations for Claude Code

- No placeholder folder currently maps cleanly to this stage. `services/workflow-orchestrator/`
  covers [Document Orchestration](03-document-orchestration.md) only (per-document). The Driver
  Orchestration is genuinely new work with no corresponding original-architecture service — see
  [Alignment gaps](#alignment-gaps-vs-current-code) for the recommended folder.
- Write a **deliberate concurrency test** before considering this stage done: release several
  documents for the same driver into the pipeline at the same instant and assert exactly one
  `driver_evaluation` reaches `DECIDED` and exactly one outbox row exists per document. Per the
  architecture document's own build-sequence guidance (§11), this is the exit criterion to be
  strictest about — "it is not visible in single-document testing, it will not appear in a demo, and
  it fails silently in production by producing nothing at all."

## Alignment gaps vs. current code

No existing service or doc models a per-driver join at all — `docs/services/workflow-orchestrator.md`
(old) describes a single per-document orchestration that calls Mercury directly for case
creation/update, with no concept of waiting for sibling documents. This entire stage is new
relative to both the original architecture and the current placeholder codebase.

**Recommendation:** add a new service folder, e.g. `services/driver-orchestrator/` (Durable
Functions, matching `workflow-orchestrator`'s compute type), rather than overloading
`workflow-orchestrator` with two unrelated `instanceId` schemes (`document_guid` vs. `driver_key`)
in one Function App. This is a repository-structure decision the team should confirm — see the
top-level `../README.md#open-questions--decisions-required`.

## Open Questions / Decisions Required

- **Join mechanism**: Service Bus session vs. Durable Entity vs. Postgres advisory lock — the
  architecture document explicitly leaves this open ("pick one and document it"). This
  documentation set assumes Service Bus sessions (consistent with the `driver-decision` queue
  config in `../message-contracts.md`) as the working default; confirm before Phase 4 build.
- **Repository structure**: new `services/driver-orchestrator/` folder vs. extending
  `services/workflow-orchestrator/` with a second orchestrator type. See
  [Alignment gaps](#alignment-gaps-vs-current-code).
- **I-10** (answered: a new DMER after a batch is posted is "treated separately") — the precise
  mechanics of "separately" (a new `driver_evaluation` row vs. reopening the existing one, and
  whether previously-`DECIDED` documents in the same driver's earlier batch get re-diffed against
  the new arrival for duplicate detection) is not fully specified. See
  `../data-model.md#open-questions--decisions-required`.
