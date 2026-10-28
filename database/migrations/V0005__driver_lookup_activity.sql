-- V0005__driver_lookup_activity.sql
--
-- What the Driver Lookup activity (docs/development/stages/03-document-orchestration.md,
-- "Activity: Driver Lookup") needs beyond V0001. Additive only (expand step).

-- Its own dmer_stage_run rows: every activity writes one (03, "Two conventions").
ALTER TYPE dmer_stage ADD VALUE IF NOT EXISTS 'DRIVER_LOOKUP' BEFORE 'NORMALIZE';

-- dmer_document ---------------------------------------------------------------
-- How driver_key was decided:
--   MERCURY_SUPPLIED -- Mercury's batch record named the driver (Ingest set it);
--   LICENCE_LOOKUP   -- the AI read the licence off the page and Mercury's
--                       GET by driver_licence found exactly one driver. The
--                       Decision Gateway records this as proposed_driver_key /
--                       MAP_DRIVER (question I-11: the AI may map automatically).
ALTER TABLE dmer_document ADD COLUMN IF NOT EXISTS driver_resolved_by text;
-- The licence read off the page differs from the Mercury-supplied driver's.
-- Mercury's driver is kept; this records the discrepancy (03, Driver Lookup
-- step 1 -- where to store it was an open question).
ALTER TABLE dmer_document ADD COLUMN IF NOT EXISTS licence_mismatch boolean;
