-- roles.sql
--
-- Grants di-processor's Managed Identity exactly the table access the
-- Extraction stage uses -- per table, not Ingest's schema-wide grant, because
-- di-processor touches only four tables:
--
--   dmer_document        read status; compare-and-set DOWNLOADED -> EXTRACTING
--                        -> EXTRACTED (the insert path of upsert_status is
--                        also granted, so no code path hits a permission error)
--   dmer_stage_run       the EXTRACT audit row: insert RUNNING, update to
--                        SUCCEEDED/FAILED, close abandoned RUNNING rows
--   dmer_extraction      upsert the extracted values + cut-off flags
--   message_idempotency  claim / complete -- and DELETE: a failed handler
--                        releases its claim by deleting the row so the message
--                        stays redrivable (dmer_common.messaging
--                        .PostgresIdempotencyStore.release). Without DELETE,
--                        every failure would itself fail with a permission
--                        error.
--
-- No default privileges: a future table di-processor needs gets an explicit
-- grant here, alongside the migration that creates it.
--
-- Prerequisites:
--   * Flyway migrations V0001-V0003 applied to `dmer` (message_idempotency is
--     V0003) -- a GRANT on a missing table fails;
--   * create-principal.sql already run against `postgres`.
-- Run against the app database (`dmer`); apply_roles.sh does this.
--
-- :"identity_name" is double-quoted: a role identifier here.

GRANT SELECT, INSERT, UPDATE ON dmer_document TO :"identity_name";
GRANT SELECT, INSERT, UPDATE ON dmer_stage_run TO :"identity_name";
GRANT USAGE, SELECT ON SEQUENCE dmer_stage_run_id_seq TO :"identity_name";
GRANT SELECT, INSERT, UPDATE ON dmer_extraction TO :"identity_name";
GRANT SELECT, INSERT, UPDATE, DELETE ON message_idempotency TO :"identity_name";
