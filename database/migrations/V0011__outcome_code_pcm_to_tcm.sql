-- V0011__outcome_code_pcm_to_tcm.sql
--
-- Renames the dmer_outcome_code enum value PCM to TCM in databases created
-- before V0001 was corrected.
--
-- V0001 originally created dmer_outcome_code with 'PCM'; it was later edited
-- in place to 'TCM' (the rule engine's outcome code -- see
-- dmer_common.rules.engine.OUTCOME_PRIORITY). Databases that had already run
-- V0001 kept 'PCM', and V0001's CREATE TYPE ... EXCEPTION WHEN
-- duplicate_object skips an existing type, so re-running it doesn't fix them.
-- The Decision Gateway writes dmer_decision.outcome_code, so a TCM outcome
-- fails to insert until this runs.
--
-- Re-runnable and a no-op where the enum already has TCM (a database created
-- from the corrected V0001). rule_evaluation.selected_outcome_code is text and
-- unaffected.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_enum
        WHERE enumtypid = 'dmer_outcome_code'::regtype AND enumlabel = 'PCM'
    ) THEN
        ALTER TYPE dmer_outcome_code RENAME VALUE 'PCM' TO 'TCM';
    END IF;
END $$;
