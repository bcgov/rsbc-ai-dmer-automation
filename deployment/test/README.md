
# test deployment parameters

Environment-specific values only (SKUs, scaling limits, resource group/VNet/subnet
references). Deployed against `infrastructure/bicep/subscription.bicep` — see
`docs/deployment/deployment-guide.md`.

## Before deploying — placeholders to fill in

- `vnetResourceGroupName` and `vnetName` — TEST's landing zone VNet has not been confirmed with
  the platform team yet. `vnetName` currently holds an *unconfirmed guess*
  (`f11861-test-vwan-spoke`, following DEV's naming) — verify it, don't deploy against it as-is.
- `privateEndpointSubnetAddressPrefix` — same guidance as `deployment/dev/README.md`.

Run `scripts/ci/check_platform_prerequisites.sh -e test -g <vnetResourceGroupName> -n <vnetName>`
once you have real values, before deploying.

## Notes

- `allowSharedKeyAccess: false` — hardened relative to DEV; if this breaks an existing script
  or manual workflow that still uses the storage account key, migrate it to Azure AD/Managed
  Identity auth rather than reverting this value.
- Run `az deployment sub what-if` before deploying here (1-approver gate) — see
  `docs/deployment/deployment-guide.md`.
- This file does *not* hold Azure OpenAI values — see `deployment/dev/README.md`.
