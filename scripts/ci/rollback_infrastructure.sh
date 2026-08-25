#!/bin/bash
#
# rollback_infrastructure.sh
#
# Deletes the DMER Automation Document Intelligence + Storage infrastructure
# for ONE environment — everything created by
# infrastructure/bicep/subscription.bicep (and, nested inside it,
# infrastructure/bicep/main.bicep). Use this to tear down a DEV/TEST
# throwaway deployment, or to clean up after a failed/half-finished
# deployment before retrying.
#
# WHAT THIS DELETES (derived directly from subscription.bicep/main.bicep —
# see docs/deployment/rollback-guide.md for the full mapping):
#
#   1. The application resource group (rg-rsbc-dmer-<env> by default) and
#      EVERYTHING inside it:
#        - Document Intelligence account (di-rsbc-dmer-shared-<env>-<instance>)
#        - Storage account + its "raw" blob container
#          (stdmer<env><region>-<instance>)
#        - Managed identity (id-rsbc-dmer-di-processor-<env>-<instance>)
#        - Both private endpoints (pe-di-..., pe-st-...)
#        - The two RBAC role assignments (they're scoped to the DI account
#          and storage account, both inside this resource group, so they're
#          removed automatically when those resources are deleted — no
#          separate role-assignment cleanup is needed)
#      This is a single `az group delete` — the safest way to guarantee
#      nothing inside is missed.
#
#   2. The private-endpoint subnet (snet-rsbc-dmer-pe-<env>-<instance>) and,
#      unless -N is passed, the NSG (nsg-rsbc-dmer-pe-<env>-<instance>) —
#      BOTH of these live in the PLATFORM TEAM'S VNet resource group, not in
#      the application resource group above, because subscription.bicep
#      deploys them scoped to vnetResourceGroupName. They must be deleted
#      individually, by exact name, and in this order (subnet before NSG —
#      an NSG can't be deleted while a subnet still references it; the
#      resource group must go first — a subnet can't be deleted while a
#      private endpoint inside it still exists).
#
# WHAT THIS NEVER TOUCHES, by construction (this script only ever
# references resource names it computed itself or that you passed
# explicitly — it never lists/deletes "everything in a resource group
# matching a pattern"):
#   - The VNet itself (e.g. f11861-dev-vwan-spoke), its peering, flow logs
#   - Any OTHER subnet in that VNet (AzureBastionSubnet, or a
#     manually-created one such as
#     snet-rsbc-dmer-ai-optimization-private-endpoints)
#   - Any resource outside the two resource groups named above
#   - Private DNS zones — these are platform-hub-managed and out of scope
#     for this script entirely (see docs/deployment/deployment-guide.md,
#     "Private DNS: confirmed behavior")
#   - Anything in a resource group you didn't pass to -r/-g
#
# This script is READ + DELETE only for the exact resources named above —
# it never modifies or deletes anything by pattern-matching or wildcard.
#
# Usage:
#   scripts/ci/rollback_infrastructure.sh \
#     -e dev \
#     -g <vnet-resource-group> \
#     -n f11861-dev-vwan-spoke \
#     [-r rg-rsbc-dmer-dev] [-i 001] [-s <subnet-name>] [-k <nsg-name>] \
#     [-d] [-y] [-R] [-S] [-N]
#
# Flags:
#   -e  Environment: dev | test | prod (required — used to compute default
#       resource names via this repo's naming convention)
#   -g  Resource group containing the platform VNet (required)
#   -n  Platform VNet name (required)
#   -r  Application resource group name (default: rg-rsbc-dmer-<env>)
#   -i  Instance suffix (default: 001)
#   -s  Private-endpoint subnet name (default: snet-rsbc-dmer-pe-<env>-<instance>)
#   -k  NSG name (default: nsg-rsbc-dmer-pe-<env>-<instance>)
#   -d  Dry run — print exactly what would be deleted, then exit. No
#       deletion, no confirmation prompt.
#   -y  Skip the interactive confirmation prompts (still prints the plan
#       first). Use for scripted/CI teardown of DEV/TEST only — never pass
#       this against PROD; the PROD-specific confirmation below is
#       intentionally not skippable by -y.
#   -R  Skip deleting the application resource group
#   -S  Skip deleting the subnet
#   -N  Skip deleting the NSG
#   -h  Show this usage and exit
#
# Requires: az CLI, logged in (`az login`) with the correct subscription
# selected (`az account set --subscription ...`) BEFORE running this.

set -euo pipefail

ENVIRONMENT=""
VNET_RG=""
VNET_NAME=""
APP_RG=""
INSTANCE="001"
SUBNET_NAME=""
NSG_NAME=""
DRY_RUN=false
SKIP_CONFIRM=false
SKIP_RG=false
SKIP_SUBNET=false
SKIP_NSG=false

usage() {
  cat >&2 <<'EOF'
Usage: rollback_infrastructure.sh -e <dev|test|prod> -g <vnet-resource-group> -n <vnet-name>
                                   [-r <app-resource-group>] [-i <instance>]
                                   [-s <subnet-name>] [-k <nsg-name>]
                                   [-d] [-y] [-R] [-S] [-N] [-h]

See the header comment in this file for what each flag does and exactly
which resources this script deletes.
EOF
  exit 1
}

while getopts "e:g:n:r:i:s:k:dyRSNh" opt; do
  case "$opt" in
    e) ENVIRONMENT="$OPTARG" ;;
    g) VNET_RG="$OPTARG" ;;
    n) VNET_NAME="$OPTARG" ;;
    r) APP_RG="$OPTARG" ;;
    i) INSTANCE="$OPTARG" ;;
    s) SUBNET_NAME="$OPTARG" ;;
    k) NSG_NAME="$OPTARG" ;;
    d) DRY_RUN=true ;;
    y) SKIP_CONFIRM=true ;;
    R) SKIP_RG=true ;;
    S) SKIP_SUBNET=true ;;
    N) SKIP_NSG=true ;;
    h) usage ;;
    *) usage ;;
  esac
done

if [[ -z "$ENVIRONMENT" || -z "$VNET_RG" || -z "$VNET_NAME" ]]; then
  usage
fi

case "$ENVIRONMENT" in
  dev|test|prod) ;;
  *)
    echo "ERROR: -e must be one of dev, test, prod (got '$ENVIRONMENT')." >&2
    exit 1
    ;;
esac

if ! command -v az >/dev/null 2>&1; then
  echo "az CLI not found — install it and run 'az login' first." >&2
  exit 1
fi

# Defaults follow docs/standards/naming-conventions.md, matching the vars
# computed in infrastructure/bicep/subscription.bicep and main.bicep. If
# your deployment used non-default names (e.g. a different -i/instance, or
# createNetworkSecurityGroup=false with an existing NSG), pass -r/-s/-k to
# override rather than editing this script.
: "${APP_RG:=rg-rsbc-dmer-$ENVIRONMENT}"
: "${SUBNET_NAME:=snet-rsbc-dmer-pe-$ENVIRONMENT-$INSTANCE}"
: "${NSG_NAME:=nsg-rsbc-dmer-pe-$ENVIRONMENT-$INSTANCE}"

info()  { echo "  $1"; }
warn()  { echo "  [WARNING] $1" >&2; }

echo "=== Current Azure context ==="
az account show --query "{subscription:name, id:id, user:user.name}" -o table
echo
echo "  Everything below is scoped to THIS subscription. If that's not the"
echo "  right one, Ctrl+C now and run 'az account set --subscription ...'."
echo

echo "=== Rollback plan for environment '$ENVIRONMENT' ==="
echo
echo "1) Application resource group (deletes everything inside it):"
if [[ "$SKIP_RG" == "true" ]]; then
  info "SKIPPED (-R passed) — '$APP_RG' and its contents will NOT be touched."
else
  info "Resource group: $APP_RG"
  RG_EXISTS=$(az group exists -n "$APP_RG" -o tsv)
  if [[ "$RG_EXISTS" == "true" ]]; then
    info "Currently contains:"
    az resource list -g "$APP_RG" --query "[].{name:name, type:type}" -o table | sed 's/^/    /'
  else
    info "Does not exist — nothing to delete here (this step will be a no-op)."
  fi
fi
echo
echo "2) Private-endpoint subnet (in the PLATFORM VNet resource group, not the"
echo "   resource group above):"
if [[ "$SKIP_SUBNET" == "true" ]]; then
  info "SKIPPED (-S passed) — '$SUBNET_NAME' will NOT be touched."
else
  info "Subnet: $SUBNET_NAME (VNet: $VNET_NAME, resource group: $VNET_RG)"
  if az network vnet subnet show -g "$VNET_RG" --vnet-name "$VNET_NAME" -n "$SUBNET_NAME" -o none 2>/dev/null; then
    info "Exists — will be deleted after the resource group above is gone."
  else
    info "Does not exist — nothing to delete here (this step will be a no-op)."
  fi
fi
echo
echo "3) Network security group (same platform VNet resource group):"
if [[ "$SKIP_NSG" == "true" ]]; then
  info "SKIPPED (-N passed) — '$NSG_NAME' will NOT be touched."
else
  info "NSG: $NSG_NAME (resource group: $VNET_RG)"
  if az network nsg show -g "$VNET_RG" -n "$NSG_NAME" -o none 2>/dev/null; then
    info "Exists — will be deleted after the subnet above."
  else
    info "Does not exist — nothing to delete here (this step will be a no-op)."
  fi
fi
echo
echo "This script will NOT touch: the VNet itself ($VNET_NAME), any other"
echo "subnet in it, anything outside '$APP_RG' and '$VNET_RG', or Private DNS"
echo "zones (platform-hub-managed, out of scope here)."
echo

if [[ "$DRY_RUN" == "true" ]]; then
  echo "--dry-run (-d): stopping here. Nothing was deleted."
  exit 0
fi

if [[ "$ENVIRONMENT" == "prod" ]]; then
  echo "You are about to delete PRODUCTION infrastructure."
  read -r -p "Type DELETE PROD to continue: " prod_confirm
  if [[ "$prod_confirm" != "DELETE PROD" ]]; then
    echo "Confirmation text did not match — aborting. Nothing was deleted." >&2
    exit 1
  fi
fi

if [[ "$SKIP_CONFIRM" != "true" ]]; then
  read -r -p "Type the application resource group name ($APP_RG) to confirm deletion: " rg_confirm
  if [[ "$rg_confirm" != "$APP_RG" ]]; then
    echo "Confirmation text did not match '$APP_RG' — aborting. Nothing was deleted." >&2
    exit 1
  fi
fi

echo
echo "=== Deleting ==="

if [[ "$SKIP_RG" == "true" ]]; then
  info "1) Resource group: skipped (-R)."
else
  RG_EXISTS=$(az group exists -n "$APP_RG" -o tsv)
  if [[ "$RG_EXISTS" == "true" ]]; then
    info "1) Deleting resource group '$APP_RG' (blocking until complete — this can take several minutes)..."
    az group delete -n "$APP_RG" --yes
    info "   Done."
  else
    info "1) Resource group '$APP_RG' does not exist — skipping (already gone)."
  fi
fi

if [[ "$SKIP_SUBNET" == "true" ]]; then
  info "2) Subnet: skipped (-S)."
else
  if az network vnet subnet show -g "$VNET_RG" --vnet-name "$VNET_NAME" -n "$SUBNET_NAME" -o none 2>/dev/null; then
    info "2) Deleting subnet '$SUBNET_NAME'..."
    az network vnet subnet delete -g "$VNET_RG" --vnet-name "$VNET_NAME" -n "$SUBNET_NAME"
    info "   Done."
  else
    info "2) Subnet '$SUBNET_NAME' does not exist — skipping (already gone)."
  fi
fi

if [[ "$SKIP_NSG" == "true" ]]; then
  info "3) NSG: skipped (-N)."
else
  if az network nsg show -g "$VNET_RG" -n "$NSG_NAME" -o none 2>/dev/null; then
    info "3) Deleting NSG '$NSG_NAME'..."
    az network nsg delete -g "$VNET_RG" -n "$NSG_NAME"
    info "   Done."
  else
    info "3) NSG '$NSG_NAME' does not exist — skipping (already gone)."
  fi
fi

echo
echo "=== Rollback complete ==="
echo "Not deleted (and never touched by this script): the VNet itself, any"
echo "other subnet in it, and any Private DNS zone/A-record the platform"
echo "team's DINE policy created for the now-deleted private endpoints —"
echo "that cleanup is theirs to own, not something this script has access to"
echo "or should attempt. Redeploying this environment later with the same"
echo "names is safe; the DINE policy will register fresh records the same"
echo "way it did the first time (~10 minutes after the new private endpoints"
echo "deploy)."
