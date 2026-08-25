#!/bin/bash
#
# check_platform_prerequisites.sh
#
# Read-only checks against Azure to answer, before a first deployment to a
# given environment: does the resource group already exist, does the
# platform-provided VNet/subnet already exist, and does the signed-in
# principal already hold the RBAC it needs (subnet-join on the VNet's
# resource group, Contributor on the target resource group)?
#
# Also attempts a best-effort search for the two Private DNS zones this
# repository's private endpoints target. For this landing zone that search
# is expected to come back empty (confirmed with the platform team: the
# zones live centrally in their hub subscription and their DINE policy
# registers DNS A-records automatically) — see
# docs/deployment/deployment-guide.md, "Private DNS: confirmed behavior".
# privateDnsZoneIdCognitiveServices/privateDnsZoneIdBlob should stay empty
# regardless of what this script reports for that section.
#
# This does not change anything — every command below is a read (`show`,
# `list`) — and it does not require the tunnel: all of it is Azure Resource
# Manager control-plane, same as `az bicep`/`az deployment group ...`. See
# docs/deployment/deployment-guide.md for the control-plane-vs-data-plane
# breakdown.
#
# Usage:
#   scripts/ci/check_platform_prerequisites.sh \
#     -e dev \
#     -g <vnet-resource-group> \
#     -n f11861-dev-vwan-spoke \
#     [-r rg-rsbc-dmer-dev] \
#     [-d <dns-resource-group>] [-s <dns-subscription-id>]
#
# Requires: az CLI, logged in (`az login`) with the correct subscription
# selected (`az account set --subscription ...`).

set -euo pipefail

ENVIRONMENT=""
VNET_RG=""
VNET_NAME=""
APP_RG=""
DNS_RG=""
DNS_SUBSCRIPTION=""

usage() {
  echo "Usage: $0 -g <vnet-resource-group> -n <vnet-name> [-e dev|test|prod] [-r <app-resource-group>] [-d <dns-resource-group>] [-s <dns-subscription-id>]" >&2
  exit 1
}

while getopts "e:g:n:r:d:s:h" opt; do
  case "$opt" in
    e) ENVIRONMENT="$OPTARG" ;;
    g) VNET_RG="$OPTARG" ;;
    n) VNET_NAME="$OPTARG" ;;
    r) APP_RG="$OPTARG" ;;
    d) DNS_RG="$OPTARG" ;;
    s) DNS_SUBSCRIPTION="$OPTARG" ;;
    h) usage ;;
    *) usage ;;
  esac
done

if [[ -z "$VNET_RG" || -z "$VNET_NAME" ]]; then
  usage
fi

if ! command -v az >/dev/null 2>&1; then
  echo "az CLI not found — install it and run 'az login' first." >&2
  exit 1
fi

pass() { echo "  [FOUND]   $1"; }
fail() { echo "  [MISSING] $1"; }
unknown() { echo "  [CHECK MANUALLY] $1"; }

echo "=== Current Azure context ==="
az account show --query "{subscription:name, id:id, user:user.name}" -o table || true
echo

echo "=== Resource group: ${APP_RG:-<not supplied, skipping>} ==="
if [[ -n "$APP_RG" ]]; then
  # -o tsv is required here, not optional: az group exists returns a bare
  # boolean, and if your az CLI's configured default output format is
  # "table" (a common interactive default — check with `az configure -l`),
  # the un-pinned output renders as a table ("Result\n--------\nTrue") which
  # never equals the literal string "true", so this would always report
  # MISSING even when the resource group exists. Every az call in this
  # script whose output feeds a comparison pins -o explicitly for this
  # reason — do not remove -o tsv/-o json/-o none below.
  if [[ "$(az group exists -n "$APP_RG" -o tsv)" == "true" ]]; then
    pass "Resource group '$APP_RG' already exists — deploy main.bicep directly at resource-group scope instead of subscription.bicep, or expect subscription.bicep's RG module to be a no-op update."
  else
    fail "Resource group '$APP_RG' does not exist yet — subscription.bicep will create it."
  fi
fi
echo

echo "=== Platform VNet: $VNET_NAME (resource group: $VNET_RG) ==="
if az network vnet show -g "$VNET_RG" -n "$VNET_NAME" -o none 2>/dev/null; then
  pass "VNet '$VNET_NAME' found in resource group '$VNET_RG'."
  echo "  Address space:"
  az network vnet show -g "$VNET_RG" -n "$VNET_NAME" --query "addressSpace.addressPrefixes" -o tsv | sed 's/^/    /'
  echo "  Existing subnets (avoid overlapping these when choosing privateEndpointSubnetAddressPrefix):"
  az network vnet subnet list -g "$VNET_RG" --vnet-name "$VNET_NAME" --query "[].{name:name, prefix:addressPrefix}" -o table | sed 's/^/    /'
else
  fail "VNet '$VNET_NAME' not found in resource group '$VNET_RG' — double check the resource group name and that you have Reader access there."
fi
echo

echo "=== RBAC: does the signed-in principal have rights on the VNet's resource group? ==="
CALLER=$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)
if [[ -z "$CALLER" ]]; then
  CALLER=$(az account show --query user.name -o tsv 2>/dev/null || true)
fi
VNET_RG_ID=$(az group show -n "$VNET_RG" --query id -o tsv 2>/dev/null || true)
if [[ -n "$VNET_RG_ID" ]]; then
  echo "  Signed-in principal: $CALLER"
  echo "  Scope checked:       $VNET_RG_ID"
  echo "  Direct role assignments at that scope:"
  az role assignment list --assignee "$CALLER" --scope "$VNET_RG_ID" --query "[].{role:roleDefinitionName, scope:scope}" -o table 2>/dev/null | sed 's/^/    /' || unknown "Could not list role assignments — check manually via Portal IAM > Check access, or ask the platform team whether you have Network Contributor / Owner (or a custom role with Microsoft.Network/virtualNetworks/subnets/join/action and .../subnets/write) on this resource group."
  unknown "This only lists DIRECT assignments at this exact scope — inherited assignments (subscription/management-group level, e.g. subscription-level Owner) won't show here. If the list above is empty but you know you have Owner/Contributor at the subscription level, that's expected and sufficient — this check is a starting point, not a definitive answer."
else
  unknown "Could not resolve the VNet resource group's ID — check RBAC manually via Portal IAM > Check access."
fi
echo

echo "=== Private DNS zones ==="
echo "  NOTE (confirmed with the platform team for this landing zone): these zones live"
echo "  centrally in the platform's hub subscription, not in this project's subscription or"
echo "  the VNet's resource group. If the search below finds nothing, that is the EXPECTED"
echo "  outcome unless you happen to have Reader on the hub subscription — it does not mean"
echo "  anything is broken or missing. Their Deploy-If-Not-Exists (DINE) policy registers each"
echo "  private endpoint's DNS A-record automatically, observed within ~10 minutes of the"
echo "  endpoint being deployed. privateDnsZoneIdCognitiveServices/privateDnsZoneIdBlob should"
echo "  stay empty regardless of what this check reports. See"
echo "  docs/deployment/deployment-guide.md, 'Private DNS: confirmed behavior', and"
echo "  https://developer.gov.bc.ca/docs/default/component/public-cloud-techdocs/azure/best-practices/be-mindful/#private-endpoints-and-dns"

# If -d/-g weren't supplied, try to locate the zones automatically across
# every subscription the signed-in principal can read, via Azure Resource
# Graph (a single cross-subscription query — much faster than looping
# `az network private-dns zone list` per subscription by hand). This needs
# the `resource-graph` CLI extension; install it silently if missing, and
# fall back to manual instructions if that fails (e.g. extension installs
# blocked by policy) or the query itself errors. Given the confirmed hub
# placement above, this search coming back empty is the common case, not a
# bug — it's kept mainly as a convenience for a future landing zone that
# manages DNS differently, or in case you do have hub-subscription access.
GRAPH_AVAILABLE=false
if [[ -z "$DNS_RG" ]]; then
  if az extension show -n resource-graph -o none 2>/dev/null || az extension add -n resource-graph -y -o none 2>/dev/null; then
    GRAPH_AVAILABLE=true
  fi
fi

DNS_ARGS=()
if [[ -n "$DNS_RG" ]]; then DNS_ARGS+=(-g "$DNS_RG"); fi
if [[ -n "$DNS_SUBSCRIPTION" ]]; then DNS_ARGS+=(--subscription "$DNS_SUBSCRIPTION"); fi

for zone in privatelink.cognitiveservices.azure.com privatelink.blob.core.windows.net; do
  echo "  --- $zone ---"

  FOUND_RG=""
  FOUND_SUB=""
  if [[ -n "$DNS_RG" ]]; then
    FOUND_RG="$DNS_RG"
    FOUND_SUB="$DNS_SUBSCRIPTION"
  elif [[ "$GRAPH_AVAILABLE" == "true" ]]; then
    # Cross-subscription search — requires at least Reader on whichever
    # subscription actually hosts the zone; Resource Graph silently omits
    # subscriptions you can't read rather than erroring.
    GRAPH_RESULT=$(az graph query -q "resources | where type =~ 'microsoft.network/privatednszones' and name =~ '$zone' | project resourceGroup, subscriptionId" --query "data[0].[resourceGroup, subscriptionId]" -o tsv 2>/dev/null || true)
    if [[ -n "$GRAPH_RESULT" ]]; then
      FOUND_RG=$(echo "$GRAPH_RESULT" | cut -f1)
      FOUND_SUB=$(echo "$GRAPH_RESULT" | cut -f2)
      pass "Located via Resource Graph: resource group '$FOUND_RG', subscription '$FOUND_SUB'."
    fi
  fi

  if [[ -n "$FOUND_RG" ]]; then
    ZONE_ARGS=(-g "$FOUND_RG")
    if [[ -n "$FOUND_SUB" ]]; then ZONE_ARGS+=(--subscription "$FOUND_SUB"); fi
    pass "Zone '$zone' exists in resource group '$FOUND_RG'."
    echo "    VNet links:"
    az network private-dns link vnet list -z "$zone" "${ZONE_ARGS[@]}" --query "[].{name:name, vnet:virtualNetwork.id, state:virtualNetworkLinkState}" -o table 2>/dev/null | sed 's/^/      /'
    LINK_ID=$(az network private-dns link vnet list -z "$zone" "${ZONE_ARGS[@]}" --query "[?contains(virtualNetwork.id, '$VNET_NAME')].name" -o tsv 2>/dev/null || true)
    if [[ -n "$LINK_ID" ]]; then
      pass "  '$VNET_NAME' is linked to this zone — privateDnsZoneId* can point at this zone's resource ID; no action needed."
    else
      fail "  '$VNET_NAME' is NOT linked to this zone yet — either ask the platform team to link it, or this repository's private endpoints will need DNS registered another way."
    fi
  elif [[ -n "$DNS_RG" ]]; then
    fail "Zone '$zone' not found in resource group '$DNS_RG'/subscription supplied — double check, or ask the platform team which subscription/resource group centrally hosts it."
  else
    echo "  [EXPECTED] Zone '$zone' not found via Resource Graph across the subscriptions you can read. For this landing zone that is normal, not an error: the platform team confirmed both zones live in their hub subscription, and Resource Graph silently omits subscriptions you don't have Reader on rather than erroring — you likely just don't have Reader there, which is fine. privateDnsZoneIdCognitiveServices/privateDnsZoneIdBlob should stay empty either way; their DINE policy handles DNS A-record creation automatically (~10 min after a private endpoint deploys) — see docs/deployment/deployment-guide.md, 'Private DNS: confirmed behavior'. If you want to verify anyway rather than take this on faith: after deploying a private endpoint, resolve its hostname from inside the VNet (e.g. from a Bastion-connected jump box) and confirm it returns a private IP in the private-endpoint subnet — that's a more reliable check than hunting for the zone resource itself."
  fi
done
echo

echo "=== Summary ==="
echo "This script only reads Azure state — it does not create or change anything."
echo "Fill in deployment/${ENVIRONMENT:-<env>}/parameters.json with the confirmed values above"
echo "(vnetResourceGroupName, vnetName, privateEndpointSubnetAddressPrefix)."
echo "Leave privateDnsZoneIdCognitiveServices/privateDnsZoneIdBlob EMPTY regardless of what the"
echo "Private DNS zones check above found — confirmed with the platform team, their DINE policy"
echo "handles DNS A-record creation automatically. See docs/deployment/deployment-guide.md,"
echo "'Private DNS: confirmed behavior', before running 'az deployment sub what-if'."
