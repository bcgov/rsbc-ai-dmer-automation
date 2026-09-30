-- V0003__create_message_idempotency.sql
--
-- Durable consumer-side message idempotency for every Service Bus consumer
-- (dmer_common.messaging.PostgresIdempotencyStore). One row per consumer
-- ("scope", e.g. 'di-processor/dmer-raw') per message.
--
-- The primary key (scope, message_id) is what makes a claim atomic: a
-- consumer claims a message with one
--   INSERT ... ON CONFLICT (scope, message_id) DO UPDATE ...
--     WHERE status = 'PROCESSING' AND lease_expires_at < now()
--   RETURNING claim_token
-- so exactly one worker runs the handler, a COMPLETED row is a duplicate, and
-- a crashed worker's PROCESSING claim can be taken over once its lease
-- expires. A row is only marked COMPLETED after the handler succeeds; a
-- failed handler deletes its claim so the message stays redrivable.
-- Semantics and crash behaviour: docs/development/message-contracts.md
-- §Message idempotency.
--
-- status is plain text (PROCESSING | COMPLETED), not an enum: the store
-- owns this vocabulary and it is not shared with other tables.
--
-- OPEN QUESTION: retention. COMPLETED rows accumulate (one per message per
-- consumer); add a purge older than the Service Bus duplicate/replay horizon.

CREATE TABLE IF NOT EXISTS message_idempotency (
    scope             text        NOT NULL,
    message_id        text        NOT NULL,
    status            text        NOT NULL,
    claim_token       text        NOT NULL,
    claimed_at        timestamptz NOT NULL,
    lease_expires_at  timestamptz NOT NULL,
    processed_at      timestamptz,
    attempts          int         NOT NULL,
    CONSTRAINT pk_message_idempotency PRIMARY KEY (scope, message_id)
);
