-- V0006__processing_error_categories.sql
--
-- processing_error's documented columns that V0001 never created
-- (docs/development/data-model.md "processing_error"; stages/10-dlq-drain.md
-- "Reason codes"). Every route to pipeline_status = MANUAL_REVIEW records a
-- row here -- the DLQ Drain, the reconciliation sweeper and the Document
-- Orchestration. Additive only (expand step).

DO $$ BEGIN
    CREATE TYPE processing_failure_category AS ENUM (
        'PERMANENT_BUSINESS', 'TRANSIENT', 'PROCESSING', 'UNKNOWN'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- The category that governs what happens next: only PERMANENT_BUSINESS may
-- accompany a dmer_decision; TRANSIENT/PROCESSING are redrive candidates;
-- UNKNOWN needs human triage.
ALTER TABLE processing_error
    ADD COLUMN IF NOT EXISTS failure_category processing_failure_category;
-- Stable enumerated reason (e.g. DRIVER_NOT_FOUND, PERMANENT_UNREADABLE_DOCUMENT,
-- NormalizeDmer:RETRIES_EXHAUSTED) -- queryable, never document content.
ALTER TABLE processing_error ADD COLUMN IF NOT EXISTS reason_code text;
-- Bounded operational-recovery redrives for TRANSIENT/PROCESSING.
ALTER TABLE processing_error ADD COLUMN IF NOT EXISTS redrive_count int NOT NULL DEFAULT 0;

-- error_class is the legacy classification (TRANSIENT/POISON/DOWNSTREAM),
-- kept for back-compat. UNKNOWN has no legacy equivalent, so it may be null.
ALTER TABLE processing_error ALTER COLUMN error_class DROP NOT NULL;
