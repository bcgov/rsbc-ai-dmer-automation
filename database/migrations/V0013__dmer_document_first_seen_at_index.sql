-- V0013__dmer_document_first_seen_at_index.sql
--
-- The Page Poller's daily limit (MERCURY_DAILY_POLL_LIMIT,
-- docs/development/stages/01-ingest.md step 1) counts the records first
-- stored since midnight on every poll:
--
--   SELECT count(*) FROM dmer_document WHERE first_seen_at >= <midnight>;
--
-- Without an index that reads the whole table each time; with it, only
-- today's rows. Additive only.

CREATE INDEX IF NOT EXISTS ix_dmer_document_first_seen_at
    ON dmer_document (first_seen_at);
