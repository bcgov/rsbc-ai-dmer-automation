
# prod deployment parameters

Environment-specific values only (SKUs, scaling limits, resource group/VNet/subnet
references). Deployed against `infrastructure/bicep/subscription.bicep` — see
`docs/deployment/deployment-guide.md`.

## Before deploying — placeholders to fill in

- `vnetResourceGroupName` and `vnetName` — PROD's landing zone VNet has not been confirmed with
  the platform team yet. `vnetName` currently holds an *unconfirmed guess*
  (`f11861-prod-vwan-spoke`, following DEV's naming) — verify it, don't deploy against it as-is.
- `privateEndpointSubnetAddressPrefix` — same guidance as `deployment/dev/README.md`.

Run `scripts/ci/check_platform_prerequisites.sh -e prod -g <vnetResourceGroupName> -n <vnetName>`
once you have real values, before deploying.

## Notes

- `allowSharedKeyAccess: false` and `disableLocalAuthDocumentIntelligence: true` — prod is
  Azure AD/Managed Identity only, no key-based fallback. Confirm every consumer (di-processor's
  managed identity, any operational scripts) is AAD-ready before the first prod deployment.
- `storageSkuName: Standard_ZRS` — zone-redundant for resilience within Canada Central; revisit
  if the platform team's DR requirements call for `Standard_GRS`/`Standard_RAGRS` (cross-region
  to Canada East) instead.
- Requires 2 approvers + a change window (see `docs/deployment/deployment-guide.md`). Always run
  `az deployment sub what-if` first and have it reviewed alongside the PR.
- This file does *not* hold Azure OpenAI values — see `deployment/dev/README.md`.
