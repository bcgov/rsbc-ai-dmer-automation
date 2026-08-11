# Naming Conventions

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

## PostgreSQL

`snake_case` tables and columns; table names are plural nouns (`documents`, `audit_log`
is the one deliberate exception, kept singular by convention for log-style tables).
