-- dmer_processing tracking table. Applied to the local Postgres instance
-- (dmer-postgres container, database "dmer") for local testing of the
-- intake-processor blob trigger.
CREATE TABLE IF NOT EXISTS dmer_processing (
    id                      BIGSERIAL PRIMARY KEY,
    dmer_id                 VARCHAR(100) UNIQUE,
    driver_license          VARCHAR(50),
    blob_path               TEXT,
    status                  VARCHAR(30),
    paddle_status           VARCHAR(30),
    di_status               VARCHAR(30),
    combined_status         VARCHAR(30),
    paddle_result_path      TEXT,
    di_result_path          TEXT,
    combined_result_path    TEXT,
    retry_count             INTEGER,
    error_message           TEXT,
    created_at              TIMESTAMP WITHOUT TIME ZONE,
    updated_at              TIMESTAMP WITHOUT TIME ZONE
);

-- Mercury backlog pagination cursor: only the "latest" (highest id) row is
-- ever acted on. `status` ('not_started' -> 'processing' -> 'finished') is
-- an audit trail of what happened to each link, not the concurrency
-- enforcement mechanism -- that's a Postgres advisory lock held for the
-- whole poll invocation (see function_app.py's _acquire_poll_lock /
-- _get_current_link / _finish_link / _mark_link_not_started).
CREATE TABLE IF NOT EXISTS mercury_links (
    id      BIGSERIAL PRIMARY KEY,
    link    TEXT NOT NULL,
    status  VARCHAR(20) NOT NULL DEFAULT 'not_started'
);
