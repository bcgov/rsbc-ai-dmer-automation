-- V0010__driver_evaluation_drop_open_constraint.sql
--
-- Second step of V0009 (read that file's header first): drops V0001's
-- UNIQUE (driver_key, open), so any number of closed (decided) batches per
-- driver is allowed. V0009's partial unique index still keeps one open
-- batch per driver.
--
-- Apply ONLY after the workflow-orchestrator code that upserts the open
-- evaluation with ON CONFLICT (driver_key) WHERE open is deployed: older
-- code's ON CONFLICT (driver_key, open) needs this constraint and fails
-- without it, sending every document to MANUAL_REVIEW from Driver Lookup.
--
-- Re-runnable (IF EXISTS).

ALTER TABLE driver_evaluation
    DROP CONSTRAINT IF EXISTS driver_evaluation_driver_key_open_key;
