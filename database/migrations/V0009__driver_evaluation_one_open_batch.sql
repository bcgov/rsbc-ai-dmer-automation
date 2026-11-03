-- V0009__driver_evaluation_one_open_batch.sql
--
-- Lets a driver's decided batch be closed, so a DMER that arrives after the
-- decision starts a new batch instead of joining one that will never be
-- decided again (question I-10: a new DMER after the outcome is posted is
-- treated separately).
--
-- V0001's UNIQUE (driver_key, open) allows one open AND one closed
-- evaluation per driver, so closing a driver's second batch would fail. It
-- is replaced by a unique index on open evaluations only: still at most one
-- open batch per driver, any number of closed ones.
--
-- Two steps, so neither the old nor the new code breaks during a deploy:
--
--   V0009 (this file): ADD the open-only index. The old constraint stays,
--     so the currently deployed Driver Lookup -- ON CONFLICT (driver_key,
--     open) -- keeps working, and the new code -- ON CONFLICT (driver_key)
--     WHERE open, used by Driver Lookup and the Rule Engine -- works too.
--   V0011: DROP the old constraint, once the new workflow-orchestrator code
--     is deployed. Until then, closing a driver's second batch is blocked
--     (the Decision Gateway's trigger deploys disabled).
--
-- Re-runnable (IF NOT EXISTS). Existing data cannot violate the new index:
-- the old constraint already allowed only one open row per driver.

CREATE UNIQUE INDEX IF NOT EXISTS ux_driver_evaluation_open_per_driver
    ON driver_evaluation (driver_key)
    WHERE open;
