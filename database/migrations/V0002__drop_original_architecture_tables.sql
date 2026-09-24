-- V0002__drop_original_architecture_tables.sql
--
-- Retires `dmer_processing` and `mercury_links` -- the original
-- architecture's tables (created ad hoc by
-- services/intake-processor/schema.sql, outside this migrations
-- convention; see V0001's header for why that migration deliberately left
-- them alone). V0001 said this explicitly: "They become dead once the
-- Ingest stage is rebuilt per docs/development/stages/01-ingest.md;
-- retire them in a later migration at that point, not this one." That
-- point is now -- the Ingest stage (services/intake-processor/function_app.py)
-- has been rebuilt against dmer_document/driver/poll_checkpoint (V0001),
-- and nothing reads or writes dmer_processing/mercury_links anymore.
--
-- schema.sql (which created these) and the two smoke-test scripts that
-- exercised the old dmer_intake trigger (tests/integration/smoke_check.py,
-- eventgrid_smoke.py) are removed in the same change that applies this
-- migration -- see that commit.

DROP TABLE IF EXISTS mercury_links;
DROP TABLE IF EXISTS dmer_processing;
