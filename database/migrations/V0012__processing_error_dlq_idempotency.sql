-- V0012__processing_error_dlq_idempotency.sql
--
-- The DLQ Drain records one processing_error row per drained dead-letter
-- message (docs/development/stages/10-dlq-drain.md "Idempotency
-- requirements"): draining the same message twice -- a run that recorded it
-- but died before completing it on Service Bus -- must not add a second row.
-- Rows without a dlq_message_id (handled errors: Driver Lookup, the
-- Document Orchestration, the sweeper) are not constrained. Additive only.

CREATE UNIQUE INDEX IF NOT EXISTS ux_processing_error_dlq_message
    ON processing_error (document_id, stage, dlq_message_id)
    WHERE dlq_message_id IS NOT NULL;
