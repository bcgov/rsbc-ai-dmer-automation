
# dev deployment parameters

**Status: deployed.** `subscription.bicep` has been successfully deployed to DEV with the
values below — `rg-rsbc-dmer-dev`, its subnet/NSG in `f11861-dev-networking`, and the full
Document Intelligence + Storage workload all exist and were verified in the Portal. This file
documents the values that were actually used, not open placeholders — treat it as the reference
for redeploying (`az deployment sub create`, safe/idempotent) or for tearing down
(`scripts/ci/rollback_infrastructure.sh`, see `docs/deployment/rollback-guide.md`).

Environment-specific values only (SKUs, scaling limits, resource group/VNet/subnet
references). Deployed against `infrastructure/bicep/subscription.bicep` (creates the resource
group + private-endpoint subnet, then the workload) — see
`docs/deployment/deployment-guide.md`. Since the resource group and subnet already exist, you
can also deploy `infrastructure/bicep/main.bicep` directly at resource-group scope instead,
passing `privateEndpointSubnetId` explicitly — see "Redeploying only the workload" in the
deployment guide.

## Confirmed values

- `vnetResourceGroupName: "f11861-dev-networking"` — confirmed via the platform team/Portal.
- `privateEndpointSubnetAddressPrefix: "10.46.156.64/27"` — confirmed non-overlapping against
  the live VNet (`AzureBastionSubnet` at `10.46.156.0/26`, and DEV's pre-existing manually
  created `snet-rsbc-dmer-ai-optimization-private-endpoints` at `10.46.156.224/27`).

`privateDnsZoneIdCognitiveServices` / `privateDnsZoneIdBlob` are confirmed empty — the platform
team centrally manages both Private DNS zones and auto-registers private endpoints via a
Deploy-If-Not-Exists policy; DEV's deployment confirmed this in practice (both private
endpoints picked up a DNS A-record). See `docs/deployment/deployment-guide.md`, "Private DNS:
confirmed behavior", for the full explanation, and to reuse this same verification process for
TEST/PROD.

`createNetworkSecurityGroup: true` creates a baseline NSG for the new subnet
(`nsg-rsbc-dmer-pe-dev-001`). If the platform team requires you to use an NSG they manage
instead, set this to `false` and supply `existingNetworkSecurityGroupId`.

## Notes

- **Two Document Intelligence + Storage pairs currently coexist in DEV, on purpose.** The old,
  manually-created `rsbc-dmer-ai-optimization-rg` (with its non-standard-named DI account and
  `rsbcstorage`) is still where model training is happening. This Bicep-managed
  `rg-rsbc-dmer-dev` is the new, standard-named pair everything will cut over to once training
  is validated — via `scripts/ci/document_intelligence_model.py copy --copy-training-data` to
  move the trained model (and optionally its training data) over, then a manual deletion of the
  old resource group once the cutover is confirmed working. Nothing in this repo's Bicep or
  scripts ever references the old resource group by name.
- `allowSharedKeyAccess: true` matches the currently-working manually-provisioned DEV storage
  account. Plan to flip this to `false` once every consumer (di-processor's managed identity,
  any scripts) authenticates via Azure AD instead of the account key.
- This file does *not* hold Azure OpenAI values — that model is hosted in a separate
  AI Hub subscription and consumed via a Key Vault secret (endpoint + API key), not a Bicep
  parameter. See `docs/architecture/repository-design.md` §10, §13. (Not yet relevant — no
  Key Vault module is deployed by this template yet.)
