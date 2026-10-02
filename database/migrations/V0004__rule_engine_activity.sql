-- V0004__rule_engine_activity.sql
--
-- What the Rule Engine activity (docs/development/stages/05-activity-rule-engine.md)
-- needs beyond V0001. Additive only (expand step): no existing column changes.

-- rule_evaluation ------------------------------------------------------------
-- rule_engine_outcome_code: the engine's own outcome. selected_outcome_code can
-- differ from it -- a clean pass that a Drugs, Alcohol and Driving row fired for
-- is selected as IN, because the 5-year driving record needs a human check
-- (05-activity-rule-engine.md, implementation considerations).
ALTER TABLE rule_evaluation ADD COLUMN IF NOT EXISTS rule_engine_outcome_code text;
-- fit_letter: a FIT letter applies to the selected outcome.
ALTER TABLE rule_evaluation ADD COLUMN IF NOT EXISTS fit_letter boolean;
-- stage_run_id: the RULES attempt that produced this evaluation. One evaluation
-- per attempt, so a retried commit cannot insert a second row; a re-run (new
-- attempt) still creates a new row, as the data model requires.
ALTER TABLE rule_evaluation
    ADD COLUMN IF NOT EXISTS stage_run_id bigint REFERENCES dmer_stage_run (id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_rule_evaluation_stage_run
    ON rule_evaluation (stage_run_id);
CREATE INDEX IF NOT EXISTS ix_rule_evaluation_document
    ON rule_evaluation (document_id, evaluated_at DESC);

-- rules_version ----------------------------------------------------------------
-- The activity identifies the active rules.json by its sha256 checksum.
CREATE INDEX IF NOT EXISTS ix_rules_version_checksum ON rules_version (checksum);

-- driver_evaluation_document ------------------------------------------------------
-- Marks a document as counted toward its driver's completed_document_count, so an
-- activity retry cannot count it twice (05-activity-rule-engine.md, idempotency):
-- the count is only incremented when this row is newly inserted.
CREATE TABLE IF NOT EXISTS driver_evaluation_document (
    driver_evaluation_id uuid        NOT NULL REFERENCES driver_evaluation (id),
    document_id          uuid        NOT NULL REFERENCES dmer_document (id),
    counted_at           timestamptz NOT NULL,
    CONSTRAINT pk_driver_evaluation_document
        PRIMARY KEY (driver_evaluation_id, document_id)
);
