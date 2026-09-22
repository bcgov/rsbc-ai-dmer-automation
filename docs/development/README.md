# DMER Intake Automation — Development Documentation

**Start here.** This is the developer-facing implementation reference for the DMER Intake
Automation pipeline, built from the **revised architecture**
(`docs/architecture/DMER_Intake_Automation_Revised_Architecture.docx` and its three figures) and
cross-checked against the current codebase. It is written so a developer — or Claude Code — can
say "implement the Ingestion stage per `docs/development/stages/01-ingest.md`" and have everything
needed without re-deriving the architecture from scratch.

**Source of truth:** the revised architecture document and figures. Where the current codebase or
older docs disagree with it, this documentation set follows the revised architecture and calls out
the disagreement explicitly in each affected file's "Alignment gaps vs. current code" section. Do
not treat `docs/architecture/repository-design.md`, `docs/services/*.md`, or
`docs/contracts/queues/*.md` as current — they describe the **original** architecture and are kept
only for history. See [Superseded documentation](#superseded-documentation) below.

## Why this revision happened

> Once this solution is in production, Intake will look only at the AI-processed queue. A DMER
> whose outcome is never written back to Mercury is invisible to both the AI queue and the manual
> queue. It is not delayed; it is lost.

Every mechanism in the revised architecture that looks like extra machinery — the outbox table, the
reconciliation sweeper, the explicit waiting state, the fallback outcome — exists to make that
failure impossible. Keep this requirement in mind when a stage doc seems to add complexity for a
rare failure case: the failure case is the point.

## Processing flow

Five stages. Stages 1–3 are **per-document** and run fully in parallel across the backlog. Stage 4
is the point where documents belonging to the same driver are joined, and it is **deliberately
serialized**. Stage 5 is not a processing stage — it's the set of components that guarantee nothing
is lost.

```
Mercury (Dynamics 365, OpenShift)
    │  batch GET / webhook
    ▼
Stage 1 — Ingest (Azure Functions)              → dmer-ingest → dmer-raw
    │
    ▼
Stage 2 — Extraction (Azure Container App, the only one)  → dmer-extracted
    │
    ▼
Stage 3 — Document Orchestration (Durable Functions, per document)
    │   Activity: Normalize → Activity: Rule Engine
    ▼
                                                  → driver-decision (session = driver_key)
Stage 4 — Driver Orchestration (Durable Functions, per driver — THE JOIN, serialized)
    │   Activity: Decision Gateway → Activity: Post-Processing
    ▼
mercury_outbox (same transaction as the decision)
    │
    ▼
Stage 5 — Reliability: Outbox Publisher, Reconciliation Sweeper, DLQ Drain
    │
    ▼
Mercury (POST/PUT outcome, case, duplicate, driver mapping)
```

Full diagram: `docs/architecture/Figure1_Revised_Architecture.png`. Database ERD:
`docs/architecture/Figure2_Database_ERD.png` (reference: [data-model.md](data-model.md)). Status
lifecycle: `docs/architecture/Figure3_Status_Lifecycle.png` (reference:
[data-model.md#status-modelling](data-model.md#status-modelling)).

## Stage documentation

| # | Stage | Compute | Doc |
|---|---|---|---|
| 1 | Ingest (Page Poller, Ingest Function, Webhook Listener) | Azure Functions | [stages/01-ingest.md](stages/01-ingest.md) |
| 2 | Extraction (DMER Extraction Processor) | Azure Container App | [stages/02-extraction.md](stages/02-extraction.md) |
| 3 | Document Orchestration | Durable Functions (per document) | [stages/03-document-orchestration.md](stages/03-document-orchestration.md) |
| 3a | Activity: Normalize | Durable activity | [stages/04-activity-normalize.md](stages/04-activity-normalize.md) |
| 3b | Activity: Rule Engine | Durable activity (in-process library) | [stages/05-activity-rule-engine.md](stages/05-activity-rule-engine.md) |
| 4 | Driver Orchestration (the join) | Durable Functions (per driver, serialized) | [stages/06-driver-orchestration.md](stages/06-driver-orchestration.md) |
| 4a | Activity: Decision Gateway | Durable activity | [stages/07-decision-gateway.md](stages/07-decision-gateway.md) |
| 4b | Activity: Post-Processing | Durable activity | [stages/08-post-processing.md](stages/08-post-processing.md) |
| 5 | Reliability: Outbox Publisher, Reconciliation Sweeper | Azure Functions (timers) | [stages/09-reliability-components.md](stages/09-reliability-components.md) |
| 5 | DLQ Drain | Azure Function | [stages/10-dlq-drain.md](stages/10-dlq-drain.md) |

## Azure service documentation

| Service | Doc |
|---|---|
| Azure Functions | [services/azure-functions.md](services/azure-functions.md) |
| Azure Service Bus | [services/azure-service-bus.md](services/azure-service-bus.md) |
| Azure Container Apps | [services/azure-container-apps.md](services/azure-container-apps.md) |
| Azure Document Intelligence | [services/azure-document-intelligence.md](services/azure-document-intelligence.md) |
| Azure OpenAI (GPT-5.1, AI Hub) | [services/azure-openai.md](services/azure-openai.md) |
| Azure Database for PostgreSQL | [services/azure-database-postgresql.md](services/azure-database-postgresql.md) |
| Azure Blob Storage | [services/azure-blob-storage.md](services/azure-blob-storage.md) |
| Azure Key Vault | [services/azure-key-vault.md](services/azure-key-vault.md) |
| Azure Monitor / Application Insights | [services/azure-monitor.md](services/azure-monitor.md) |

## Cross-cutting references

- [data-model.md](data-model.md) — every PostgreSQL table, the status/enum model, indexes. Every
  stage doc links here instead of restating columns.
- [message-contracts.md](message-contracts.md) — the four Service Bus queues, the single message
  envelope shape, dead-letter classification. Every stage doc links here instead of restating queue
  config.

## What changed from the original architecture

The pipeline shape, queue-decoupled stages, and blob-per-stage artifacts are retained. The revision
addresses four things the original architecture left open:

| Area | Change | Why |
|---|---|---|
| Decision gateway | Split into a per-document orchestration and a per-driver orchestration, serialized per driver | In the original design, two documents finishing at the same time either both stood down (outcome never posted) or both proceeded (conflicting outcomes posted). See [Driver Orchestration](stages/06-driver-orchestration.md#why-serialization-is-required). |
| Waiting state | "Not all documents ready" is an explicit `driver_evaluation` row, not an implicit do-nothing branch | A row can be found by a sweeper and reported on; a silent return cannot. |
| Ingest | Split into a Page Poller and a per-document Ingest Function | One bad document in a page of fifty shouldn't fail/re-download the other forty-nine. |
| Driver resolution | Moved from the decision gateway into Extraction | Documents must be grouped by driver from the start, including when Mercury supplies no driver object. |
| Cut-off detection | Performed in Extraction, persisted as three flags (`has_header`/`has_signature`/`is_cutoff`) | Cheap geometric check on OCR output; needed as a Decision Gateway input, not a late re-read. |
| Normalizer | A Durable Functions activity calling Azure OpenAI directly — **not a Container App** | The endpoint is reachable with a key; a container adds deployment/networking with no benefit. |
| Rule engine | An in-process library (GoRules/Zen) inside a Durable activity — **not a separate service** | It's a library, not a service. |
| Database | Per-stage status columns replaced by a `dmer_stage_run` audit table + two denormalized pointers on `dmer_document` | Per-stage columns lose attempt history and force a migration for every new stage. |
| Mercury write-back | `mercury_outbox` table, written in the same transaction as the decision, drained by a publisher | Guarantees the decision and the intent to publish it can't diverge. |
| Failure handling | The DLQ Drain classifies each dead-letter (permanent-business / transient / processing / unknown); only a **permanent-business** failure produces a fallback outcome (`IN`), the rest go to manual review / operational recovery | Failing to decide must not mean failing to appear — but a transient fault must not masquerade as a decision. See [ADR-0002](../architecture/decision-records/0002-dlq-fallback-decisions-gated-by-failure-category.md). |
| Queue naming | The queue between Ingest and Extraction is `dmer-raw` (not `dmer-extract`) | It carries documents *awaiting* extraction, not documents already extracted. |
| Reconciliation | Added a sweeper function and a daily report against Mercury | Turns "we believe nothing is stuck" into something demonstrable. |

See each stage/service doc's "Alignment gaps vs. current code" section for exactly what this means
for the placeholder code already in the repository.

## Implementation dependencies

The current state of the codebase, verified against the placeholder folders and `libs/dmer_common`:

- **`libs/dmer_common` is real, tested code — reuse what's architecture-agnostic, replace what
  isn't.** Reusable as-is: `messaging/{publisher,consumer,idempotency}.py` (settlement logic,
  idempotency abstraction), `telemetry/logging.py` (structured JSON logging, correlation
  propagation, PII redaction), `retry/{policies,circuit_breaker}.py`, `storage/client.py`
  (`BlobClient`), `doc_intelligence/client.py`, `openai_client/client.py`, `config/__init__.py`.
  Needs rework: `db/{documents,status}.py` (wrong table/schema — see
  [data-model.md](data-model.md#alignment-gaps-vs-current-code)),
  `storage/{containers,paths}.py` (wrong container layout — see
  [azure-blob-storage.md](services/azure-blob-storage.md#alignment-gaps-vs-current-code)),
  `dto/messages.py` (wrong queues/shape — see
  [message-contracts.md](message-contracts.md#alignment-gaps-vs-current-code)). Empty stubs needing
  real implementation: `mercury_client/__init__.py`, `auth/__init__.py`.
- **All seven `services/*/` folders are placeholders** (`raise NotImplementedError` or an empty
  `FunctionApp()`) — there is no working pipeline logic to preserve anywhere in `services/`. This
  means the folder-structure decisions below are low-risk to make now, before any real code exists
  to migrate.
- **`database/`, `infrastructure/bicep/modules/servicebus/*`, and
  `infrastructure/bicep/modules/storage/*` have no concrete instances yet** — no migrations, no
  queues/containers provisioned in `main.bicep`. There is no infrastructure drift to reconcile,
  only documentation and shared-library code.

## Recommended build order

Per the architecture document's own build sequence (§11), ordered so each phase is independently
testable and the riskiest unknowns are hit early:

| Phase | Scope | Exit criteria |
|---|---|---|
| 1 | Schema, Mercury integration spike, [Ingest](stages/01-ingest.md) | Poller and Ingest Function move documents from the batch API into `raw-dmer` and Postgres idempotently; re-running the poller creates no duplicates; questions M-1 to M-4 answered. |
| 2 | [Extraction](stages/02-extraction.md) | Combined JSON produced for a representative sample including cut-off and handwriting-heavy forms; cut-off flags validated against a manually labelled set; `driver_key` resolution working when Mercury returns no driver. |
| 3 | [Document Orchestration](stages/03-document-orchestration.md) | Normalize and Rule Engine activities running end to end; `rule_evaluation` populated with full outcome lists and `rules_version`. |
| 4 | [Driver Orchestration](stages/06-driver-orchestration.md) | The join proven **under concurrency**: a driver with several documents finishing simultaneously produces exactly one evaluation and one set of decisions. Test this deliberately with a load harness, not incidentally. |
| 5 | [Reliability](stages/09-reliability-components.md), [DLQ Drain](stages/10-dlq-drain.md) | Mercury outage simulated and recovered with no lost outcome; a **permanent-business** poison document (unreadable PDF) dead-lettered, drained, and delivered as fallback `IN`; a **transient** dead-letter (e.g. lock expiry) drained to `MANUAL_REVIEW` with **no** decision and redriven successfully. |
| 6 | Backlog drain and tuning | Quota-bounded backfill running at the agreed rate (~1M documents, oldest-first, per question I-15) alongside real-time submissions. |

**Phase 4's exit criterion is the one to be strict about.** The concurrency race in the driver join
is not visible in single-document testing, will not appear in a demo, and fails silently in
production by producing nothing at all. Write a deliberate test that releases several documents for
the same driver into the pipeline at the same instant and asserts exactly one `driver_evaluation`
reaches `DECIDED` and exactly one outbox row exists per document.

## Business rules already decided (from Intake, question set I-1 to I-17)

These are answered in the architecture document and are treated as settled in every stage doc that
implements them — don't re-litigate them without a reason:

- Fallback outcome is `IN` with comment "AI could not process," written back to Mercury (I-1) —
  **only** for a permanent-business failure (an un-processable DMER), never for a transient,
  processing, or unknown failure (see [ADR-0002](../architecture/decision-records/0002-dlq-fallback-decisions-gated-by-failure-category.md)).
- AI never revises a decision after a human has reviewed it (I-2).
- Rule engine decides PR/PU/PCM/CR entirely from medical content; `document_priority` never
  overrides it (I-3).
- Cut-off precedence: clear scan wins over a newer cut-off one *if content agrees*; disagreement
  routes to manual review (I-4).
- All-cut-off batches go to manual review with the cut-off reason surfaced (I-5).
- Differing DMERs: process the newest, mark the older duplicate/rejected — **pending final Intake
  confirmation** (I-6).
- "Duplicate" maps to Mercury's `Rejected` status (I-7).
- `Rejected` documents count toward "all documents processed" (I-8).
- Empty `dps_date` is a reliable "not yet triaged" signal (I-9).
- A new DMER arriving after a batch is posted is evaluated separately, not by reopening the batch
  (I-10) — mechanics not fully specified, see [data-model.md](data-model.md#open-questions--decisions-required).
- AI may map an unmapped driver automatically via the Mercury API (I-11).
- An unresolvable/ambiguous licence match goes to manual review with the licence info in the
  comment (I-12).
- Case creation: create if none open, otherwise attach to the existing open case and check for
  duplicate/cut-off scenarios; always state the reason in the comment (I-13).
- Backlog is ~1,000,000 documents, oldest-first (I-15).
- Blob retention: 30–90 days for now (I-17).

Full detail and the exact business framing lives in the relevant stage doc (mostly
[Decision Gateway](stages/07-decision-gateway.md) and [Post-Processing](stages/08-post-processing.md)) —
this list is a quick-reference index, not a replacement for reading them.

## Superseded documentation

These describe the **original** architecture and should not be used for new implementation work.
They are kept for history; do not delete without team agreement, but do not update them further —
update the `docs/development/` set instead.

- `docs/architecture/repository-design.md` — original 7-service repository design.
- `docs/architecture/solution-architecture.md` — points at the original architecture docx.
- `docs/services/*.md` — one file per original-architecture service.
- `docs/contracts/queues/*.md` — original 2-queue + 1-topic contracts.
- `docs/standards/naming-conventions.md` — Service Bus naming examples use the original queue
  names; the resource-naming pattern itself (§12 of `repository-design.md`) is unaffected and still
  applies.
- `docs/operations/monitoring-alerts.md` — infra-level alerts (Function failure rate, Container App
  restarts, PostgreSQL CPU/storage) are still valid; queue-specific alerts need the name updates in
  [services/azure-monitor.md](services/azure-monitor.md#alignment-gaps-vs-current-code).

## Open Questions / Decisions Required

Consolidated from every stage/service doc — see each doc for full context.

### Repository structure (no equivalent risk in the architecture document itself — these are ours to decide)

The original architecture's seven services do not map one-to-one onto the revised five-stage
design. Since every `services/*/` folder is currently an empty placeholder, this is low-risk to
decide now:

| Original folder | Revised architecture role | Recommendation |
|---|---|---|
| `services/intake-processor/` | [Ingest](stages/01-ingest.md) — largely unchanged in shape | Keep; add Page Poller + Ingest Function + Webhook Listener triggers. |
| `services/di-processor/` | [Extraction](stages/02-extraction.md) — same compute type, substantially different internal logic | Keep; rebuild internals per the stage doc. |
| `services/workflow-orchestrator/` | [Document Orchestration](stages/03-document-orchestration.md) + its two activities only | Keep, but scope it down — case creation and Mercury write-back move out (see below). |
| `services/normalizer-service/` | Folded into Document Orchestration as [Activity: Normalize](stages/04-activity-normalize.md) — no longer a Container App | Retire, or repurpose the folder for shared normalization-schema code imported by `workflow-orchestrator`. |
| `services/rule-engine/` | Folded into Document Orchestration as [Activity: Rule Engine](stages/05-activity-rule-engine.md) — no longer an HTTP Function | Retire as a standalone Function App; keep `rules/README.md`'s local sample-rules convention regardless. |
| `services/post-processing/` | Folded into [Driver Orchestration](stages/06-driver-orchestration.md) as [Activity: Post-Processing](stages/08-post-processing.md) — no longer a topic subscriber | Repurpose/move into a new driver-orchestration service (see below) — its current role (topic-triggered audit writer) no longer exists. |
| `services/audit-service/` | No equivalent in the revised architecture (`dmer_stage_run` etc. are the audit trail directly) | Open decision: retire, or repurpose as a thin read API over the new audit tables for Mercury's Review Dashboard. |
| *(new)* | [Driver Orchestration](stages/06-driver-orchestration.md), [Decision Gateway](stages/07-decision-gateway.md), [Post-Processing](stages/08-post-processing.md) | Add `services/driver-orchestrator/` — no original-architecture folder covers this at all. |
| *(new)* | [Reliability Components](stages/09-reliability-components.md), [DLQ Drain](stages/10-dlq-drain.md) | Add `services/reliability/` (three timer functions) — or fold into an existing Functions app; not specified by the architecture document. |

**This table is a recommendation, not a decision** — confirm the folder plan with the team before
Phase 1 build, since it touches CI/CD workflows (`.github/workflows/*.yml`), Bicep Function App
instantiations, and CODEOWNERS.

### Data model

- `driver_evaluation (driver_key, open)` conflict target — what "open" means is unspecified. See
  [data-model.md](data-model.md#open-questions--decisions-required).
- `dmer_decision.outcome_code` values (`CP`, `IN`, `PR`, `PU`, `PCM`, `CR`) are never defined in the
  architecture document — source definitions from Intake before building the rule engine's outcome
  table.
- Normalized clinical JSON in Postgres — needs privacy/security sign-off before adding the column.

### Mercury integration (unanswered questions from the Mercury/Dynamics platform team)

- **M-1** — pre-signed URL TTL and fresh-URL-on-demand availability. Blocks whether DLQ replay works
  by resubmission or must always re-poll Mercury. See [Ingest](stages/01-ingest.md#failure-handling)
  and [DLQ Drain](stages/10-dlq-drain.md#pre-signed-url-replay-caveat).
- **M-6** — does Mercury's POST/PUT honour an idempotency key, and what's the duplicate-submit
  behaviour? See [Post-Processing](stages/08-post-processing.md#open-questions--decisions-required).
- **M-10** — expected concurrent load / throttling on Mercury's write APIs. Sets the Outbox
  Publisher's batch size. See
  [Reliability Components](stages/09-reliability-components.md#configuration--environment-variables).

### Join mechanism

Service Bus session vs. Durable Entity vs. Postgres advisory lock for the per-driver serialization —
the architecture document deliberately leaves this open. This documentation set assumes Service Bus
sessions throughout (matches the `driver-decision` queue config); confirm before Phase 4. See
[Driver Orchestration](stages/06-driver-orchestration.md#the-mechanism).
