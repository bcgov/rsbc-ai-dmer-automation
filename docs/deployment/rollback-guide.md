
# Rollback Guide

How to tear down the Document Intelligence + Storage infrastructure
(`infrastructure/bicep/subscription.bicep`, and the `main.bicep` workload it
deploys as a nested module) for one environment, using
`scripts/ci/rollback_infrastructure.sh`.

## When to use this

- A DEV/TEST experiment you're done with and want to remove cleanly.
- A failed or half-finished deployment you want to clear out before retrying
  `az deployment sub create` from scratch.
- Decommissioning an environment.

This is **not** what you want for "redeploy with different settings" —
`az deployment sub create` (or `what-if` first) is idempotent and will
update resources in place; you don't need to delete anything first just to
change a parameter value. Only reach for rollback when you actually want
the resources gone.

## What gets deleted, and why the script needs two resource groups

`subscription.bicep` creates resources in two different resource groups,
and the rollback script has to know about both:

| Resource | Created by | Lives in |
|---|---|---|
| Document Intelligence account | `main.bicep` → `modules/ai/document-intelligence.bicep` | Application RG (`rg-rsbc-dmer-<env>`) |
| Storage account + `raw` blob container | `main.bicep` → `modules/storage/storage-account.bicep` + `blob-containers.bicep` | Application RG |
| Managed identity | `main.bicep` → `modules/identity/managed-identity.bicep` | Application RG |
| Both private endpoints | `main.bicep` → `modules/networking/private-endpoint.bicep` | Application RG |
| 2 RBAC role assignments (Cognitive Services User, Storage Blob Data Reader) | `main.bicep` | Scoped directly to the DI account / storage account, both in the Application RG |
| Application resource group itself | `subscription.bicep` → `modules/shared/resource-group.bicep` | — |
| Private-endpoint subnet | `subscription.bicep` → `modules/networking/subnet.bicep` | **Platform VNet's resource group** (e.g. `f11861-dev-networking`) — scoped there deliberately, since the subnet has to live inside the platform-owned VNet |
| NSG for that subnet | `subscription.bicep` → `modules/networking/network-security-group.bicep` (only if `createNetworkSecurityGroup: true`) | **Platform VNet's resource group** |

Everything in the first six rows is inside one resource group, so deleting
that resource group (`az group delete`) removes all of it in one step —
including the two role assignments, since a role assignment scoped to a
resource is removed automatically when that resource is deleted. That's the
"safest approach" the script uses for that half of the teardown: one
guaranteed-complete deletion instead of enumerating every resource type by
hand.

The subnet and NSG are different: they're deployed scoped to
`vnetResourceGroupName` — the **platform team's** resource group, which
also contains the VNet itself and potentially other unrelated
platform-owned resources. `az group delete` is never used there. Instead
the script deletes the subnet and NSG individually, by their exact
computed names, and in a specific order:

1. Application resource group first — this removes the private endpoints
   that live inside the subnet. A subnet can't be deleted while a private
   endpoint still references it.
2. Subnet second — once nothing references the NSG anymore.
3. NSG third — an NSG can't be deleted while a subnet is still associated
   with it.

## What this script will never touch

Every resource this script deletes is identified by an exact name computed
from `docs/standards/naming-conventions.md` (or a name you pass explicitly
via `-r`/`-s`/`-k`) — there is no wildcard or pattern-based deletion
anywhere in it. Concretely, it never touches:

- The platform VNet itself (`f11861-<env>-vwan-spoke`), its peering, flow
  logs, or Network Watcher.
- Any other subnet in that VNet — `AzureBastionSubnet`, or (in the DEV
  history behind this repo) the old manually-created
  `snet-rsbc-dmer-ai-optimization-private-endpoints`.
- The old manually-created DEV resources this repo's Bicep deliberately
  never references either (`rsbc-dmer-ai-optimization-rg` and everything in
  it) — the script has no parameter that could even point at them.
- Private DNS zones or their A-records. These are platform-hub-managed
  (see `docs/deployment/deployment-guide.md`, "Private DNS: confirmed
  behavior") — deleting a private endpoint typically cleans up its own
  DNS record as a side effect, but the zone itself is never something this
  script reads, writes, or has access to.
- Anything outside the two resource groups you pass via `-r`/`-g`.

## Usage

```bash
az login
az account set --subscription "<the environment's subscription>"

# See the plan without deleting anything:
scripts/ci/rollback_infrastructure.sh \
  -e dev \
  -g f11861-dev-networking \
  -n f11861-dev-vwan-spoke \
  -d

# Actually delete (interactive — asks you to re-type the resource group
# name to confirm):
scripts/ci/rollback_infrastructure.sh \
  -e dev \
  -g f11861-dev-networking \
  -n f11861-dev-vwan-spoke
```

Full flag reference is in the script's own header comment
(`scripts/ci/rollback_infrastructure.sh`); the essentials:

| Flag | Meaning |
|---|---|
| `-e dev\|test\|prod` | Required. Used to compute the default resource names. |
| `-g` | Required. Resource group containing the platform VNet. |
| `-n` | Required. Platform VNet name. |
| `-r` | Override the application resource group name (default `rg-rsbc-dmer-<env>`). |
| `-i` | Override the instance suffix (default `001`). |
| `-s` / `-k` | Override the subnet / NSG name, if you didn't use the defaults. |
| `-d` | Dry run — print the plan and exit. No prompt, no deletion. |
| `-y` | Skip the interactive re-type confirmation. **Never combine with a PROD run** — the PROD-specific `DELETE PROD` prompt is a separate check that `-y` does not bypass. |
| `-R` / `-S` / `-N` | Skip deleting the resource group / subnet / NSG respectively, if you only want to tear down part of it (e.g. keep the subnet+NSG, remove only the workload). |

## Safety checks built in

- Prints the current `az account show` context before doing anything, so
  you can catch "wrong subscription" before it matters.
- Prints the full plan — including, for the resource group, a live listing
  of what's actually inside it right now — before asking for confirmation.
- Every resource is existence-checked first; deleting an environment that's
  already partially or fully torn down is a safe no-op, not an error, for
  each step independently.
- Requires typing the exact application resource group name to proceed
  (not just y/N) — this catches the most likely operator mistake (running
  it against the wrong environment) before it becomes irreversible.
- `-e prod` requires a second, separate confirmation (`DELETE PROD`,
  case-sensitive, exact match) that `-y` does not skip.
- No step ever operates on more than the one exact resource named — there
  is nothing in this script that could "spread" to unrelated resources
  even under an operator mistake in the flags, other than pointing `-g`/`-n`
  at the wrong VNet resource group (which is why the plan is always printed
  and confirmed before anything happens).

## After rollback

- The application resource group, subnet, and NSG are gone. Redeploying the
  same environment later (`az deployment sub create` against
  `subscription.bicep`) recreates all three from scratch with the same
  names — this is safe and expected, there's no leftover state to conflict
  with.
- Any trained Document Intelligence models on the deleted account are gone
  too — there's no separate model-level backup here. If you need to keep a
  model, copy it out first with `scripts/ci/document_intelligence_model.py
  copy` (optionally with `--copy-training-data`) to another account before
  rolling back.
- The Private DNS A-record(s) the platform's DINE policy created for the
  deleted private endpoints are not something this script manages — expect
  the platform team's tooling to reconcile that on its own; it's not
  something you need to chase down manually.
