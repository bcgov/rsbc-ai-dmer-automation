-- V0001__create_dmer_pipeline_schema.sql
--
-- The revised DMER Intake Automation architecture's full schema -- every
-- table in docs/development/data-model.md, derived from
-- docs/architecture/DMER_Intake_Automation_Revised_Architecture.docx §7 and
-- Figure2_Database_ERD.png. See that doc for the full column-by-column
-- rationale; this file follows it exactly and does not repeat the "why"
-- inline except where a genuinely open question affects the DDL itself
-- (flagged below with OPEN QUESTION).
--
-- What this deliberately did NOT touch at the time: `dmer_processing` and
-- `mercury_links` (created ad hoc by the original architecture's
-- single-function intake, outside this migrations convention), left in
-- place per database/migrations/README.md's expand/contract rule until the
-- Ingest stage was actually rebuilt against this schema. That happened --
-- see V0002__drop_original_architecture_tables.sql, which retires both.
--
-- gen_random_uuid() is a Postgres core built-in since v13 -- no CREATE
-- EXTENSION needed (confirmed: this server runs Postgres 18).

-- ---------------------------------------------------------------------------
-- Enums
-- ---------------------------------------------------------------------------

-- Postgres has no CREATE TYPE IF NOT EXISTS -- this DO-block idiom makes
-- re-running the migration a no-op instead of an error, matching the
-- CREATE TABLE IF NOT EXISTS style already used elsewhere in this repo.
DO $$ BEGIN
    CREATE TYPE dmer_stage AS ENUM (
        'INGEST', 'EXTRACT', 'NORMALIZE', 'RULES', 'DECISION', 'POST'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE dmer_stage_run_status AS ENUM ('RUNNING', 'SUCCEEDED', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE dmer_pipeline_status AS ENUM (
        'RECEIVED', 'DOWNLOADED', 'EXTRACTING', 'EXTRACTED', 'NORMALIZED',
        'RULES_APPLIED', 'AWAITING_DRIVER_COMPLETION', 'DECIDED', 'POSTING',
        'COMPLETED', 'MANUAL_REVIEW'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE driver_evaluation_status AS ENUM (
        'WAITING', 'STALE', 'READY', 'EVALUATING', 'DECIDED', 'POSTED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- OPEN QUESTION (data-model.md): outcome code business definitions are
-- referenced throughout the architecture doc but never defined there.
-- Source from Intake before this enum is relied on for real decisions.
DO $$ BEGIN
    CREATE TYPE dmer_outcome_code AS ENUM ('CP', 'IN', 'PR', 'PU', 'PCM', 'CR');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE dmer_decided_by AS ENUM ('AI', 'FALLBACK', 'MANUAL');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE mercury_outbox_operation AS ENUM (
        'UPDATE_OUTCOME', 'MARK_DUPLICATE', 'MAP_DRIVER', 'CREATE_CASE'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE mercury_outbox_status AS ENUM ('PENDING', 'SENT', 'FAILED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE processing_error_class AS ENUM ('TRANSIENT', 'POISON', 'DOWNSTREAM');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ---------------------------------------------------------------------------
-- driver -- created before dmer_document, which FKs into it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS driver (
    driver_key      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Normalized (uppercase, punctuation stripped) before the uniqueness
    -- check -- enforced by the writer (Ingest/Extraction), not by a DB
    -- constraint, since normalization is a business rule, not a storage one.
    licence_number  text UNIQUE NOT NULL,
    mercury_driver_id text,
    first_name      text,
    last_name       text,
    last_synced_at  timestamptz
);

-- ---------------------------------------------------------------------------
-- dmer_document -- one row per document_guid (Mercury's identifier).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dmer_document (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Delivery idempotency key, not a content key -- see data-model.md's
    -- "document_guid is not a content key" section. A re-uploaded PDF may
    -- get a new guid while belonging to the same driver/case.
    document_guid           uuid UNIQUE NOT NULL,
    document_name           text,
    mercury_document_status text,
    document_priority       text,
    received_date           timestamptz,
    dps_date                timestamptz,
    queue                   text,
    business_area           text,
    mercury_case_id         text,
    driver_key              uuid REFERENCES driver (driver_key),
    -- Mercury's pre-signed source URL, from the batch GET response. Not
    -- part of the Service Bus message envelope -- message-contracts.md's
    -- one shared envelope shape has no field for it (its own document_url
    -- doesn't exist yet when the Page Poller publishes to dmer-ingest, and
    -- blob_url is documented as pointing at OUR OWN artifacts, not
    -- Mercury's). The Ingest Function re-reads it from here instead --
    -- resolves 01-ingest.md's "on the message (or re-read from
    -- dmer_document)" hedge in favour of the latter. Left populated after
    -- download (not nulled out) so a DLQ replay/re-poll has something to
    -- fall back to, pending question M-1's answer on presigned URL TTL.
    document_url            text,
    raw_blob_url            text,
    pipeline_status         dmer_pipeline_status NOT NULL DEFAULT 'RECEIVED',
    current_stage           dmer_stage NOT NULL DEFAULT 'INGEST',
    attempt_count           int NOT NULL DEFAULT 0,
    first_seen_at           timestamptz NOT NULL DEFAULT now(),
    -- Set on every write to this row, by every stage -- what the
    -- reconciliation sweeper's stall-detection query scans.
    updated_at              timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- dmer_stage_run -- the audit trail. One row per document, per stage, per
-- attempt. Every stage writes one of these (insert RUNNING, update to
-- SUCCEEDED/FAILED).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dmer_stage_run (
    id              bigserial PRIMARY KEY,
    document_id     uuid NOT NULL REFERENCES dmer_document (id),
    stage           dmer_stage NOT NULL,
    status          dmer_stage_run_status NOT NULL,
    attempt_no      int NOT NULL,
    started_at      timestamptz,
    ended_at        timestamptz,
    output_blob_url text,
    model_version   text,
    error_code      text,
    error_detail    text
);

-- ---------------------------------------------------------------------------
-- driver_evaluation -- the join unit; makes "waiting on siblings" explicit
-- and queryable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS driver_evaluation (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_key                uuid NOT NULL REFERENCES driver (driver_key),
    status                    driver_evaluation_status NOT NULL DEFAULT 'WAITING',
    -- OPEN QUESTION (data-model.md): the architecture doc specifies
    -- `INSERT ... ON CONFLICT (driver_key, open) DO UPDATE` but never
    -- defines what "open" means. Implemented here as the most literal
    -- reading -- a plain boolean, true while this evaluation is the active
    -- one for the driver -- so the conflict target below matches the doc
    -- exactly. Revisit before Phase 4 build if the real semantics turn out
    -- to be richer (e.g. a partial unique index on
    -- `status NOT IN ('DECIDED', 'POSTED')` instead of this column).
    open                       boolean NOT NULL DEFAULT true,
    expected_document_count   int,
    completed_document_count  int NOT NULL DEFAULT 0,
    last_mercury_check_at     timestamptz,
    evaluated_at              timestamptz,
    decision_summary          jsonb,
    UNIQUE (driver_key, open)
);

-- ---------------------------------------------------------------------------
-- dmer_extraction -- 1:1 with dmer_document; the extracted values that need
-- to be queried (full extraction JSON lives in the extracted-dmer blob).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dmer_extraction (
    document_id          uuid PRIMARY KEY REFERENCES dmer_document (id),
    licence_number_read  text,
    exam_date            date,
    physician_name       text,
    -- Deliberately three separate flags, not collapsed -- Intake needs to
    -- know which half of the form is missing.
    has_header           boolean,
    has_signature        boolean,
    is_cutoff            boolean,
    page_count           int,
    confidence_avg       numeric,
    -- The canonicalized subset used for duplicate detection (trimmed,
    -- lowercased, normalized dates/numbers).
    comparison_fields    jsonb,
    -- sha256 of comparison_fields -- makes duplicate detection a hash
    -- lookup instead of a blob diff.
    comparison_hash      char(64)
);

-- ---------------------------------------------------------------------------
-- rules_version -- one row per published rules.json, so a past decision can
-- be reproduced exactly. Created before rule_evaluation, which FKs into it.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rules_version (
    version       text PRIMARY KEY,
    blob_url      text,
    checksum      char(64),
    activated_at  timestamptz,
    activated_by  text
);

-- ---------------------------------------------------------------------------
-- rule_evaluation -- one row per evaluation, not per document; a re-run
-- creates a new row, nothing is overwritten.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rule_evaluation (
    id                    bigserial PRIMARY KEY,
    document_id           uuid NOT NULL REFERENCES dmer_document (id),
    rules_version         text NOT NULL REFERENCES rules_version (version),
    -- Every candidate outcome the engine returned, with its inputs -- not
    -- just the winner.
    all_outcomes          jsonb,
    selected_outcome_code text,
    selected_reason       text,
    priority_rank         int,
    evaluated_at          timestamptz
);

-- ---------------------------------------------------------------------------
-- dmer_decision -- the final per-document outcome. Written once, atomically
-- with the mercury_outbox row, by Post-Processing -- never by the Decision
-- Gateway directly.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dmer_decision (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id               uuid NOT NULL REFERENCES dmer_document (id),
    driver_evaluation_id      uuid NOT NULL REFERENCES driver_evaluation (id),
    outcome_code               dmer_outcome_code NOT NULL,
    is_duplicate              boolean NOT NULL DEFAULT false,
    -- The other document this one duplicates -- a second FK to
    -- dmer_document (alongside document_id above), not a self-reference on
    -- this table itself.
    duplicate_of_document_id  uuid REFERENCES dmer_document (id),
    duplicate_reason          text,
    superseded_by_cutoff_rule boolean NOT NULL DEFAULT false,
    driver_mapped             boolean NOT NULL DEFAULT false,
    -- Set when Mercury had no driver object but a licence resolved to
    -- exactly one driver (question I-11). Not a FK in data-model.md's own
    -- listing -- left that way here too.
    proposed_driver_key       uuid,
    decision_reason           jsonb,
    -- A fallback outcome must never be mistaken for a considered one.
    decided_by                dmer_decided_by NOT NULL,
    decided_at                timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- mercury_outbox -- the delivery guarantee. One row per intended Mercury
-- operation, written in the same transaction as dmer_decision.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS mercury_outbox (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id      uuid NOT NULL REFERENCES dmer_document (id),
    operation        mercury_outbox_operation NOT NULL,
    payload          jsonb NOT NULL,
    -- Sent to Mercury on every attempt so a retried POST/PUT is safe
    -- (pending question M-6: does Mercury actually honour it?).
    idempotency_key  text UNIQUE NOT NULL,
    status           mercury_outbox_status NOT NULL DEFAULT 'PENDING',
    attempt_count    int NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz,
    last_error       text,
    sent_at          timestamptz,
    mercury_response jsonb
);

-- ---------------------------------------------------------------------------
-- processing_error -- the failure register, populated by DLQ Drain and by
-- handled errors (e.g. the reconciliation sweeper detecting a stall).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS processing_error (
    id            bigserial PRIMARY KEY,
    document_id   uuid NOT NULL REFERENCES dmer_document (id),
    stage         dmer_stage NOT NULL,
    error_class   processing_error_class NOT NULL,
    message       text,
    dlq_message_id text,
    occurred_at   timestamptz NOT NULL DEFAULT now(),
    resolved_at   timestamptz,
    resolution    text
);

-- ---------------------------------------------------------------------------
-- poll_checkpoint -- where the poller got to, per source (BACKLOG/REALTIME).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS poll_checkpoint (
    source              text PRIMARY KEY,
    -- Resolved data-model.md's open question on this column's type: the
    -- architecture doc's ERD showed `last_page int`, but Mercury's actual
    -- pagination (question M-2, confirmed) is cursor/nextLink-based, not a
    -- page number -- the Page Poller follows Mercury's own `nextLink` URL
    -- directly rather than reconstructing query params, so what needs
    -- persisting between invocations is that URL (or null, between poll
    -- cycles), not a page index.
    last_cursor         text,
    last_received_date  timestamptz,
    last_run_at          timestamptz
);

-- ---------------------------------------------------------------------------
-- Indexes that matter (data-model.md's own list; document_guid and
-- licence_number's UNIQUE constraints above already create their indexes).
-- ---------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS ix_dmer_document_status_updated
    ON dmer_document (pipeline_status, updated_at);
CREATE INDEX IF NOT EXISTS ix_dmer_document_driver_status
    ON dmer_document (driver_key, pipeline_status);
CREATE INDEX IF NOT EXISTS ix_driver_evaluation_status_check
    ON driver_evaluation (status, last_mercury_check_at);
CREATE INDEX IF NOT EXISTS ix_mercury_outbox_pending
    ON mercury_outbox (status, next_attempt_at) WHERE status = 'PENDING';
CREATE INDEX IF NOT EXISTS ix_dmer_extraction_comparison_hash
    ON dmer_extraction (comparison_hash);
CREATE INDEX IF NOT EXISTS ix_dmer_stage_run_replay
    ON dmer_stage_run (document_id, stage, attempt_no DESC);
