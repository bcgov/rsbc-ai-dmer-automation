
# audit-service

**Azure compute:** Azure Container Apps (read API)

## Responsibilities

- Read-only query API over PostgreSQL audit/history tables
  (`audit_log`, `processing_history`, `rule_execution`, `normalization_results`).
- Backs the Review Dashboard implemented inside Mercury (per the architecture document,
  the dashboard itself lives in Mercury, not Power BI) and any operational tooling.
- Deliberately separated from `post-processing` (which owns the write path) to keep a
  clean CQRS-style read/write boundary and an independent scaling/security profile for
  externally-queried data.

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/audit-service/` for the required keys.
