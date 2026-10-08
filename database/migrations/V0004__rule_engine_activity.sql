-- V0004__rule_engine_activity.sql
--
-- What the Rule Engine activity (docs/development/stages/05-activity-rule-engine.md)
-- needs beyond V0001. Additive only (expand step).

-- driver_evaluation_document ------------------------------------------------------
-- Marks a document as counted toward its driver's completed_document_count, so the
-- count can't be incremented twice for one document (05-activity-rule-engine.md,
-- idempotency): the count is only incremented when this row is newly inserted. A
-- document evaluated again -- a redrive, a reprocess, a rules re-run -- would
-- otherwise count twice, and the driver's batch would look complete while one of
-- its documents is still in flight.
CREATE TABLE IF NOT EXISTS driver_evaluation_document (
    driver_evaluation_id uuid        NOT NULL REFERENCES driver_evaluation (id),
    document_id          uuid        NOT NULL REFERENCES dmer_document (id),
    counted_at           timestamptz NOT NULL,
    CONSTRAINT pk_driver_evaluation_document
        PRIMARY KEY (driver_evaluation_id, document_id)
);
