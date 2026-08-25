# CI/CD helper scripts

Scripts invoked from `.github/workflows/*` — e.g. per-service change detection
(build only affected services), DLQ redrive tooling, release notes generation.

- `check_platform_prerequisites.sh` — read-only checks (no changes made) against Azure: does
  the target resource group/VNet/subnet already exist, are the Private DNS zones this repo's
  private endpoints need already present and linked to the VNet, and what RBAC does the
  signed-in principal already have. Run before a first `az deployment sub create` to a given
  environment — see `docs/deployment/deployment-guide.md`.
- `document_intelligence_model.py` — builds (trains) or copies a Document Intelligence custom
  model. This is a data-plane operation the Bicep templates cannot perform — see
  `docs/deployment/deployment-guide.md` for the full explanation and its network requirements
  (must run with a route to the account's private endpoint; no public network access).
  Idempotent by default: both `build` and `copy` first check whether a model with `--model-id`
  already exists (on the target account, for `copy`) and skip rather than fail with a 409 if it
  does; pass `--force` to delete the existing model first and recreate it under the same ID.
  `copy --copy-training-data --source-container-url ... --target-container-url ...` additionally
  copies the labelled training data (documents + `fields.json`/`.ocr.json`/`.labels.json`) from
  the source account's blob container to the target's, so a later retrain against the target
  account has the same source data available — useful when cutting an environment over to a
  new, Bicep-managed Document Intelligence + Storage pair. This needs `azure-storage-blob`
  (a new dependency — add it wherever `azure-ai-documentintelligence`/`azure-identity` are
  already pinned for this project), Storage Blob Data Reader on the source account and Storage
  Blob Data Contributor on the target account for whichever identity runs it, and network
  line-of-sight to both accounts' blob private endpoints. Existing blobs at the target are
  skipped unless `--force` is set.
- `rollback_infrastructure.sh` — deletes the infrastructure `subscription.bicep`/`main.bicep`
  create for one environment: the application resource group (and everything in it) plus the
  private-endpoint subnet and NSG in the platform VNet's resource group. Parameterized for
  dev/test/prod, existence-checks every resource before deleting it (safe to re-run), and
  requires typing the exact resource group name to confirm (plus a separate `DELETE PROD` gate
  for `-e prod`). Never touches the VNet itself, other subnets, or Private DNS zones. See
  `docs/deployment/rollback-guide.md` for the full explanation and `-d` for a dry run.
