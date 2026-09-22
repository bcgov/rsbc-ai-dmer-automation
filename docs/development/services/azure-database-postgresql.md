# Azure Database for PostgreSQL (Flexible Server)

Source: architecture doc §3.2, §7, §8. Bicep modules:
`infrastructure/bicep/modules/database/{postgresql-flexible-server,postgresql-database}.bicep`.
Full schema: `../data-model.md` (this file covers the Azure resource/access-pattern side; don't
duplicate table definitions here).

## Role

**System of record for processing state, artifacts, decisions and audit — not for documents or
case management.** Mercury remains the system of record for those. Eleven tables; see
`../data-model.md` for the full reference.

## Configuration

Private endpoint; VNet-integrated; Azure AD (managed identity) authentication enabled; zone-redundant
HA in prod. Sized per environment in `deployment/<env>/parameters.json`.

## Access pattern

Every service accesses PostgreSQL through **managed identity token auth**, via SQLAlchemy/asyncpg —
see `libs/dmer_common/src/dmer_common/db/`. No connection strings in configuration.

## Outbox pattern

The core reliability mechanism (architecture doc §8.1) lives here: a decision (`dmer_decision`) and
the intent to publish it (`mercury_outbox`) commit in **one transaction** — see
[Post-Processing](../stages/08-post-processing.md#database-writes---the-transaction). Because both
statements are in one transaction, a crash either leaves nothing or leaves both; there is no state
in which a decision exists but nothing is trying to deliver it. This is the single most important
transactional invariant in the schema — any new write path that touches `dmer_decision` must also
write (or already have written) a corresponding `mercury_outbox` row in the same transaction.

## Indexes

See `../data-model.md#indexes-that-matter` for the full list (idempotent ingest, sweeper scan,
driver completeness checks, outbox publisher poll, duplicate detection, stage-run replay). These
back the specific queries each reliability/stage doc describes — don't add a query pattern in a new
stage without checking whether it needs a matching index here first.

## Read access pattern (reporting / dashboards)

The original architecture had a dedicated read-only `audit-service` Container App backing Mercury's
Review Dashboard. The revised architecture does not mention a replacement, because `dmer_stage_run`
plus the other audit-oriented tables (`processing_error`, `rule_evaluation`, `dmer_decision`) are
now directly the audit trail — see [Alignment gaps](#alignment-gaps-vs-current-code) for what that
means for the `audit-service` folder.

## Data protection

- DMER content and extraction/normalization output are personal/medical information. Encryption at
  rest (platform default) and in transit (TLS 1.2+) apply as everywhere else, but the schema
  deliberately **keeps clinical content out of Postgres** wherever possible — `dmer_extraction`
  stores only the comparison field subset plus its hash, not the full extracted/normalized JSON
  (that lives in blob storage; see `azure-blob-storage.md`).
- Whether normalized clinical JSON may additionally live as `jsonb` in Postgres (for reporting/rule
  tuning) is an **open question requiring privacy/security sign-off** — see
  `../data-model.md#open-questions--decisions-required`. Do not add such a column speculatively.
- Retention: 30–90 days for raw/extracted/normalized blob artifacts (question I-17, answered) — the
  Postgres rows referencing them (via `dmer_stage_run.output_blob_url`, etc.) should not outlive a
  deleted blob without at least recording that the blob is gone; confirm the retention/lifecycle
  policy's interaction with the audit trail before implementing blob lifecycle rules.

## Authentication / identity

Managed identity, AAD role membership per service (least privilege — e.g. the Outbox Publisher
needs write access to `mercury_outbox`/`dmer_document`/`driver_evaluation` but has no reason to
write `rule_evaluation`). Define per-service DB roles rather than one shared role for every
Function App/Container App.

## Migrations

`database/migrations/` currently holds only a `README.md` placeholder — no forward-only migrations
exist yet. `database/schema/` and `database/seed/dev/` are similarly placeholder-only. The full
eleven-table schema in `../data-model.md` needs its first migration written before Phase 1
(architecture doc's build sequence, §11) can start.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/db/documents.py` and `status.py` need to be **replaced**, not
  extended — see `../data-model.md#alignment-gaps-vs-current-code` for exactly what changes
  (table name, column set, the two-field `current_stage`/`pipeline_status` model instead of one
  `DocumentStatus` enum). The `is_valid_transition`/pure-transition-logic pattern in `status.py` is
  worth keeping as a template for the new state machines.
- No repository exists yet for `dmer_stage_run`, `driver`, `driver_evaluation`, `dmer_extraction`,
  `rule_evaluation`, `rules_version`, `dmer_decision`, `mercury_outbox`, `processing_error`, or
  `poll_checkpoint` — ten repositories to build, following `documents.py`'s
  SQLAlchemy-Core-plus-async-engine pattern (pure logic separated from the async driver so it's
  unit-testable without a live database).
- Build a shared `dmer_stage_run` start/succeed/fail helper (used identically by every stage — see
  `../data-model.md#dmer_stage_run`) rather than letting each stage hand-roll its own
  insert-then-update sequence.

## Alignment gaps vs. current code

The original architecture's `audit-service` (read-only Container App API over `audit_log`,
`processing_history`, `rule_execution`, `normalization_results` — none of which exist in the
revised schema) has no equivalent in the revised architecture. `services/audit-service/` is a
placeholder Container App folder (`main.py` raises `NotImplementedError`) with no working code to
preserve. Whether to retire it, or repurpose it as a thin read-only API over the *new* audit tables
(`dmer_stage_run`, `processing_error`, `rule_evaluation`, `dmer_decision`) for Mercury's Review
Dashboard, is an open decision — see the top-level `../README.md#open-questions--decisions-required`.
