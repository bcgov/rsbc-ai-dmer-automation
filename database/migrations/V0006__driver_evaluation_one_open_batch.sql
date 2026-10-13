-- V0006__driver_evaluation_one_open_batch.sql
--
-- Lets a driver's decided batch be closed, so a DMER that arrives after the
-- decision starts a new batch instead of joining one that will never be
-- decided again (question I-10: a new DMER after the outcome is posted is
-- treated separately).
--
-- V0001's UNIQUE (driver_key, open) allows one open AND one closed
-- evaluation per driver, so closing a driver's second batch would fail.
-- Replaced by a unique index on open evaluations only: still at most one
-- open batch per driver, any number of closed ones.
--
-- Writers that upsert the open evaluation (Driver Lookup, Rule Engine) use
-- ON CONFLICT (driver_key) WHERE open, which this index serves.
--
-- Re-runnable: IF EXISTS / IF NOT EXISTS. Existing data cannot violate the
-- new index -- the old constraint already allowed one open row per driver.

CREATE UNIQUE INDEX IF NOT EXISTS ux_driver_evaluation_open_per_driver
    ON driver_evaluation (driver_key)
    WHERE open;

ALTER TABLE driver_evaluation
    DROP CONSTRAINT IF EXISTS driver_evaluation_driver_key_open_key;
