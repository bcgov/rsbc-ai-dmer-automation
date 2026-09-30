# Naming Conventions

> **Partially superseded.** The resource-naming *pattern* below is still current. The Service Bus
> entity **examples** (`raw-dmer-queue`, `dmer-lifecycle-events`, `sub-post-processing`) are from
> the original architecture — see
> [`docs/development/message-contracts.md`](../development/message-contracts.md) for the revised
> four-queue topology (`dmer-ingest`, `dmer-raw`, `dmer-extracted`, `driver-decision`; no topics).

See `docs/architecture/repository-design.md` for the full Azure resource naming convention
table. Summary pattern:

```
<resource-type-abbreviation>-rsbc-dmer-<service-or-shared>-<environment>-<instance>
```

Examples: `func-rsbc-dmer-intake-processor-dev-001`, `sb-rsbc-dmer-shared-prod-001`,
`psql-rsbc-dmer-shared-test-001`.

## Service Bus entities

`kebab-case`, `<domain>-<stage>-queue` for queues (`raw-dmer-queue`), `<domain>-events` for
topics (`dmer-lifecycle-events`), `sub-<consumer>` for subscriptions (`sub-post-processing`).

## Blob containers

Lowercase, singular-noun, matching pipeline stage: `raw`, `ocr`, `normalized`, `rules`,
`audit`, `failed`, `archive`.

Documented exception: the di-processor extraction-output container `extracted-dmer` (all
per-document extraction artifacts, including the combined extraction) deliberately deviates
from the singular-noun rule to match the pipeline stage name. See the di-processor ADR and `docs/architecture/repository-design.md` §10/§11.

## PostgreSQL

`snake_case` tables and columns; table names are plural nouns (`documents`, `audit_log`
is the one deliberate exception, kept singular by convention for log-style tables).
