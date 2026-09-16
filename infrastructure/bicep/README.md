
# Infrastructure (Bicep)

Three entry points:

- `subscription.bicep` (subscription scope) — creates the application resource group and the
  private-endpoint subnet (+ NSG) inside the existing platform VNet, then deploys `main.bicep`
  as a nested module. Use this for a fresh environment.
- `main.bicep` (resource group scope) — the workload itself (Managed Identity, Document
  Intelligence, Storage). Deployable standalone once the resource group and subnet already
  exist.
- `intake-processor.bicep` (resource group scope) — intake-processor's own Function App +
  hosting plan + Application Insights. Deployed separately from `main.bicep` because DEV's
  intake-processor resources already live in a different resource group
  (`rsbc-dmer-ai-optimization-rg`) than `main.bicep`'s usual target (`rg-rsbc-dmer-dev`) — see
  the header comment in `intake-processor.bicep` for the full reasoning and what would change if
  these resource groups are ever consolidated. Uses the same shared `modules/compute/function-app.bicep`
  and `modules/monitor/application-insights.bicep` that every other service's compute eventually
  will (docs/architecture/repository-design.md §10) — only the top-level orchestration is
  separate, not the modules.

Both compose the reusable modules in `modules/`. Per-environment values live in
`deployment/<env>/parameters.json` (not here) — this folder contains templates only, per
"do not duplicate templates".

## Current scope

Implemented: application resource group, a private-endpoint subnet (+ NSG) inside the existing
platform VNet, Managed Identity (`id-rsbc-dmer-di-processor-<env>-001`), Document Intelligence
(`di-rsbc-dmer-shared-<env>-001`), Storage Account + blob containers (`stdmer<env>cac001`),
private endpoints for both, and the RBAC role assignments that let the managed identity call
Document Intelligence and read storage blobs. This is the slice DEV was manually provisioned
with, plus resource group/subnet automation added afterwards — see
`docs/architecture/repository-design.md` for the full, eventual pipeline (Service Bus,
PostgreSQL, Key Vault, App Configuration, Container Apps, Function Apps, Monitor), which is not
wired into `main.bicep` yet.

Still platform-team-owned, not created here: the landing zone VNet itself
(`f11861-<env>-vwan-spoke`), its peering to the hub, flow logs, and Network Watcher. The
**Document Intelligence Studio project, labeling configuration, and custom model
training/publishing are also not Bicep resources** — they are data-plane operations against
the account this template creates. See `docs/deployment/deployment-guide.md` for what they
require instead (`scripts/ci/document_intelligence_model.py`) and the full
control-plane-vs-data-plane / tunnelling analysis.

See `docs/architecture/repository-design.md` for the module list, resource naming
convention, and dependency diagram.

## Networking prerequisites

`subscription.bicep` requires `vnetResourceGroupName` and `vnetName` — the existing
platform-provided VNet (`f11861-dev-vwan-spoke` in DEV; TEST/PROD unconfirmed) — and
`privateEndpointSubnetAddressPrefix`, a CIDR that doesn't overlap any existing subnet in that
VNet. None of these are created by this template; run
`scripts/ci/check_platform_prerequisites.sh` first to check what's already known/confirmed
rather than guessing — see `docs/deployment/deployment-guide.md`. Deployment fails fast with
placeholder values (`deployment/<env>/parameters.json` ships with
`__PLATFORM_TEAM_TO_SUPPLY__` / `__VERIFY_AGAINST_VNET_ADDRESS_SPACE__`) rather than silently
deploying against an empty/invalid value.

`privateDnsZoneIdCognitiveServices` / `privateDnsZoneIdBlob` should stay empty (their default)
for this project — **confirmed with the platform team**, not just assumed: both Private DNS
zones already exist centrally in the landing zone hub, and their Deploy-If-Not-Exists (DINE)
policy registers each private endpoint's DNS A-record automatically (~10 minutes after
deployment). See `docs/deployment/deployment-guide.md`, "Private DNS: confirmed behavior", and
`docs/deployment/environment-setup.md` for the full explanation and citation. Only set these
params if a future landing zone doesn't provide the same automation.

## Validating locally

```bash
# Bicep CLI only (no Azure login required for build/lint):
az bicep build --file subscription.bicep
az bicep lint --file subscription.bicep

# Requires Azure login + subscription-scope deployment (see docs/deployment/deployment-guide.md):
az deployment sub what-if \
  --location canadacentral \
  --template-file subscription.bicep \
  --parameters ../../deployment/dev/parameters.json
```

For `intake-processor.bicep` (resource-group scope — no `--location` flag, and the two
`@secure()` parameters are never in the parameters file, only passed inline from a pipeline
secret or your own shell):

```bash
az bicep build --file intake-processor.bicep
az bicep lint --file intake-processor.bicep

STORAGE_CS=$(az storage account show-connection-string -n rsbcstorage \
  -g rsbc-dmer-ai-optimization-rg --query connectionString -o tsv)

az deployment group what-if \
  --resource-group rsbc-dmer-ai-optimization-rg \
  --template-file intake-processor.bicep \
  --parameters @../../deployment/dev/intake-processor.parameters.json \
  --parameters storageAccountConnectionString="$STORAGE_CS" mercuryApiKey="<the real key>"
```
