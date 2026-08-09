
# Migrations

Numbered, forward-only SQL migrations (e.g. Flyway/Alembic-style naming:
`V0001__create_documents_table.sql`, `V0002__create_document_hash_table.sql`, ...).

## Rules

- Every migration is backward-compatible with the previous service release (expand/contract
  pattern) so a service rollback never requires a schema rollback — see
  `docs/deployment/rollback-procedures.md`.
- No destructive changes (`DROP COLUMN`, `DROP TABLE`) in the same release that stops writing
  to them — deprecate first, drop in a later release.
- Migrations run via the CI/CD pipeline against the target environment's PostgreSQL Flexible
  Server using the deployment Managed Identity (AAD auth, no passwords).
