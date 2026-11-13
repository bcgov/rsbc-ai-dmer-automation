# dmer_common tests

Unit tests for the shared library, mirroring the `src/dmer_common/` module layout.

- `integration/` -- the DB repositories against a real PostgreSQL; skipped unless
  `POSTGRES_TEST_DSN` is set.
- `live/` -- normalization end to end against the real Azure OpenAI deployment
  (Section D terms one per document and four per document, plus the normalization
  checklist). Skipped unless `DMER_LIVE_TESTS=1` and the Azure OpenAI settings are set.
  These cost money, take several minutes, and vary from run to run; see
  `live/test_live_normalization.py`.
