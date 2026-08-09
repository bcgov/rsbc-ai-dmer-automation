
# Rollback Procedures

## Services (Functions / Container Apps)

Both Azure Functions and Container Apps support revision/slot-based rollback:

- Container Apps: shift traffic back to the previous healthy revision (`az containerapp revision set-mode` / traffic split).
- Function Apps: redeploy the previous build artifact from the pipeline's stored artifacts.

## Infrastructure

Bicep deployments are declarative — to roll back, redeploy the previous commit's templates
and parameters. Use `az deployment group what-if` before any rollback deployment to prod.

## Database

Migrations must be written to be backward-compatible for at least one release (expand/contract
pattern) so a service rollback never requires a schema rollback. See `database/migrations/README.md`.
