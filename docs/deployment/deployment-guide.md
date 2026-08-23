
# Deployment Guide

This is the reusable, step-by-step procedure for deploying the Document
Intelligence + Storage infrastructure (`infrastructure/bicep/`) to any
environment. It was written after DEV's first successful deployment and is
meant to be followed the same way for TEST, PROD, or any future
environment — only the values in `deployment/<env>/parameters.json`
change, never the Bicep templates themselves.

## Environments

| Environment | Purpose | Approval |
|---|---|---|
| dev | Continuous deployment from `main` | Automatic |
| test | Pre-production validation, UAT | 1 approver |
| prod | Production | 2 approvers, change window |

## Architecture at a glance

Two Bicep entry points, deployed as one command:

- **`infrastructure/bicep/subscription.bicep`** (subscription scope) —
  creates the application resource group, and a private-endpoint subnet
  (+ NSG) inside the **existing, platform-provided** VNet. Then invokes
  `main.bicep` as a nested module.
- **`infrastructure/bicep/main.bicep`** (resource group scope) — the
  workload: Managed Identity, Document Intelligence account, Storage
  account + blob container, both private endpoints, and the RBAC role
  assignments connecting them.

You normally deploy `subscription.bicep` directly; it pulls in `main.bicep`
for you (see "Redeploying only the workload" below for the one case where
you'd invoke `main.bicep` on its own).

Everything this creates, by resource group:

```
<vnetResourceGroupName>              (platform-owned, pre-existing — never created by this repo)
  └─ <VNet, e.g. f11861-dev-vwan-spoke>   (platform-owned, pre-existing)
       └─ subnet: snet-rsbc-dmer-pe-<env>-<instance>        ← created by subscription.bicep
  └─ nsg: nsg-rsbc-dmer-pe-<env>-<instance>                 ← created by subscription.bicep (optional)

rg-rsbc-dmer-<env>                   ← created by subscription.bicep
  ├─ id-rsbc-dmer-di-processor-<env>-<instance>      (Managed Identity)
  ├─ di-rsbc-dmer-shared-<env>-<instance>             (Document Intelligence)
  │    └─ pe-di-rsbc-dmer-shared-<env>-<instance>     (its private endpoint)
  ├─ stdmer<env><region>-<instance>                   (Storage Account)
  │    ├─ raw                                          (blob container)
  │    └─ pe-st-rsbc-dmer-shared-<env>-<instance>     (its private endpoint)
  └─ 2 RBAC role assignments (Cognitive Services User, Storage Blob Data Reader)
       scoped to the DI account / storage account, granted to the Managed Identity
```

Not created anywhere in this repository: the VNet itself and its peering
(platform team), Private DNS zones (platform team — see "Private DNS:
confirmed behavior" below), and the Document Intelligence Studio
project/labeling/custom model training (data-plane, not an ARM concept —
see "Document Intelligence Studio, labeling, and custom models" below).

## Prerequisites

- Azure CLI with the Bicep extension: `az bicep install` (first time) or
  `az bicep upgrade` (keep current).
- An identity with: Contributor (or equivalent) at the subscription level,
  to create the application resource group; and rights on the **platform
  VNet's resource group** to create a subnet and NSG there
  (`Microsoft.Network/virtualNetworks/subnets/join/action` +
  `.../subnets/write`, typically via Network Contributor — don't assume
  subscription-level Contributor implies this; it's a separate,
  platform-owned resource group).
- Four values from the platform team, per environment (see the table
  below): the VNet name, the resource group that contains it, a free CIDR
  range for the new subnet, and confirmation of how they handle Private
  DNS registration (already confirmed for this landing zone — see below;
  re-verify if you're reusing this repo against a different landing zone).

## Environment-specific values

Everything that differs between DEV/TEST/PROD lives in
`deployment/<env>/parameters.json` — never in the `.bicep` files
themselves. This is the complete list of what to check/fill in before a
first deployment to a given environment.

| Parameter | DEV (deployed) | TEST | PROD | Where it comes from |
|---|---|---|---|---|
| `environment` | `dev` | `test` | `prod` | Fixed per environment |
| `location` | `canadacentral` | `canadacentral` | `canadacentral` | BC Gov landing zones are Canada-only; only `canadacentral`/`canadaeast` are allowed |
| `instance` | `001` | `001` | `001` | Only changes if you ever deploy a second parallel instance in the same environment |
| `resourceGroupName` | `rg-rsbc-dmer-dev` | `rg-rsbc-dmer-test` | `rg-rsbc-dmer-prod` | Computed from the naming convention — free to choose, but keep this pattern |
| `vnetResourceGroupName` | `f11861-dev-networking` (confirmed) | **ask the platform team** | **ask the platform team** | Platform team |
| `vnetName` | `f11861-dev-vwan-spoke` (confirmed) | **ask the platform team** — placeholder guesses `f11861-test-vwan-spoke` | **ask the platform team** — placeholder guesses `f11861-prod-vwan-spoke` | Platform team |
| `privateEndpointSubnetAddressPrefix` | `10.46.156.64/27` (confirmed against the live VNet) | **verify against that environment's VNet address space** | **verify against that environment's VNet address space** | Computed by you, from the platform-provided VNet's free address space — see "Choosing a subnet CIDR" below |
| `createNetworkSecurityGroup` | `true` | `true` | `true` | Set `false` + supply `existingNetworkSecurityGroupId` only if the platform team mandates their own NSG |
| `existingNetworkSecurityGroupId` | `""` | `""` | `""` | Only used when the above is `false` |
| `privateDnsZoneIdCognitiveServices` / `privateDnsZoneIdBlob` | `""` | `""` | `""` | **Confirmed empty for this landing zone** — see "Private DNS: confirmed behavior" below. Only set these if a future landing zone doesn't provide the same DINE-policy automation |
| `documentIntelligenceSku` | `S0` | `S0` | `S0` | Change only if you need a different Cognitive Services tier |
| `storageSkuName` | `Standard_LRS` | `Standard_LRS` | `Standard_ZRS` | Redundancy — higher in PROD |
| `blobContainerNames` | `["raw"]` | `["raw"]` | `["raw"]` | Add more container names to the array if needed |
| `allowSharedKeyAccess` | `true` | `false` | `false` | DEV allows account-key auth for convenience; TEST/PROD require Azure AD only |
| `disableLocalAuthDocumentIntelligence` | `false` | `false` | `true` | PROD disables API-key auth on the DI account entirely |
| `costCenter` / `owner` / `dataClassification` | `RSBC` / `RSBC-DMER` / `protected-b` | same | same | Tag values — change if these aren't accurate for your deployment |

Values that are **never** in a parameter file, because they're specific to
*how* you run the command, not to the environment's infrastructure:

- The Azure subscription you target (`az account set --subscription ...`).
- The `--location` flag on the deployment command itself — this is where
  the subscription-scope deployment's own metadata/history is stored, and
  is independent of the `location` parameter (which controls where
  resources actually go).

### Choosing a subnet CIDR

Before filling in `privateEndpointSubnetAddressPrefix` for a new
environment:

```bash
az network vnet show -g <vnetResourceGroupName> -n <vnetName> --query "addressSpace.addressPrefixes" -o tsv
az network vnet subnet list -g <vnetResourceGroupName> --vnet-name <vnetName> --query "[].{name:name, prefix:addressPrefix}" -o table
```

Pick a range that doesn't overlap anything in that list. A `/27` (32
addresses) is what DEV uses and is plenty for this repo's two private
endpoints plus headroom for future services
(`docs/architecture/repository-design.md` §11 lists what's still coming —
Service Bus, PostgreSQL, Key Vault, App Configuration — each of which would
add one more private endpoint to the same subnet).
`scripts/ci/check_platform_prerequisites.sh` (see below) automates the two
commands above.

## Deployment procedure

Run these in order, from the repository root, on a machine with `az` CLI
installed and network access to `management.azure.com` (this entire
procedure is Azure Resource Manager **control-plane** — no tunnel or VNet
route is needed for any step here; see "Is the tunnel required?" below for
why).

**1. Authenticate and select the subscription**

```bash
az login
az account set --subscription "<environment's subscription name or ID>"
az account show --query "{subscription:name, id:id, user:user.name}" -o table
```

Confirm the printed subscription matches the environment you intend to
deploy to before continuing.

**2. Fill in `deployment/<env>/parameters.json`**

Using the table above — for a brand-new environment, everything under
"Platform team" needs to be obtained first; `<env>/README.md` documents
the same placeholders inline.

**3. Local validation (no Azure calls)**

```bash
az bicep upgrade
az bicep build --file infrastructure/bicep/subscription.bicep
az bicep lint --file infrastructure/bicep/subscription.bicep
```

No output from the last two commands means success — they only print on
error or warning.

**4. Check what already exists**

```bash
scripts/ci/check_platform_prerequisites.sh \
  -e <env> \
  -g <vnetResourceGroupName> \
  -n <vnetName> \
  -r rg-rsbc-dmer-<env>
```

Read-only — reports whether the application resource group already exists
(if so, you'd deploy `main.bicep` directly instead — see below), the VNet's
address space and existing subnets, and a best-effort RBAC check. See the
script's own header comment for the full explanation, including why its
Private DNS zone search is expected to come back empty for this landing
zone.

**5. `what-if` — see the plan before anything changes**

```bash
az deployment sub what-if \
  --location <location> \
  --template-file infrastructure/bicep/subscription.bicep \
  --parameters deployment/<env>/parameters.json
```

Expect roughly 11 resources, all `+ Create`: the application resource
group, NSG, subnet, managed identity, Document Intelligence account, its
private endpoint, storage account, blob container, its private endpoint,
and 2 role assignments. Anything else — especially a `~ Modify` or
`- Delete`, or anything referencing a resource you didn't expect this
template to touch — means stop and investigate before proceeding.

Worth specifically checking on the storage account's plan: its
`networkAcls` block should show `defaultAction: "Deny"`, `bypass:
"AzureServices"`, and a `resourceAccessRules` entry whose `resourceId`
points at that environment's Document Intelligence account. That's the ARM
equivalent of the Portal's "Enabled from selected networks" + Resource
Instance rule, and is required (not optional) for Document Intelligence to
read training data from the storage account — see the header comment in
`modules/storage/storage-account.bicep` for the full explanation.

**6. Deploy**

```bash
az deployment sub create \
  --location <location> \
  --template-file infrastructure/bicep/subscription.bicep \
  --parameters deployment/<env>/parameters.json
```

Typically takes 3–8 minutes. `provisioningState: "Succeeded"` in the output
means it worked; the `outputs` block gives you
`documentIntelligenceEndpoint`, `documentIntelligenceName`,
`storageAccountDeployedName`, `resourceGroupName`, and
`privateEndpointSubnetId` — keep these, you'll want the DI endpoint later
for `scripts/ci/document_intelligence_model.py`.

This command is safe to re-run if it fails partway through — Azure Resource
Manager deployments are idempotent; re-running picks up where it left off
rather than duplicating anything.

**7. Wait for DNS registration (~10 minutes)**

The platform's Deploy-If-Not-Exists (DINE) policy creates each private
endpoint's DNS A-record automatically, but not instantly. Don't be
surprised if the endpoints aren't resolvable immediately after step 6 —
see "Private DNS: confirmed behavior" below.

**8. Verify in the Portal**

See "Post-deployment Portal verification" below.

### Redeploying only the workload

If the application resource group and subnet already exist (step 4 above
reported `FOUND` for the resource group), you can redeploy just the
Document Intelligence/Storage/identity workload directly against
`main.bicep` instead of going through `subscription.bicep` again:

```bash
az deployment group what-if \
  --resource-group rg-rsbc-dmer-<env> \
  --template-file infrastructure/bicep/main.bicep \
  --parameters environment=<env> location=<location> instance=001 \
  --parameters privateEndpointSubnetId=<subnet-resource-id> \
  --parameters privateDnsZoneIdCognitiveServices="" privateDnsZoneIdBlob="" \
  --parameters documentIntelligenceSku=S0 storageSkuName=Standard_LRS \
  --parameters blobContainerNames='["raw"]' \
  --parameters allowSharedKeyAccess=true disableLocalAuthDocumentIntelligence=false \
  --parameters costCenter=RSBC owner=RSBC-DMER dataClassification=protected-b

# Repeat with `create` in place of `what-if` once the plan looks right.
```

`deployment/<env>/parameters.json` is written for `subscription.bicep`'s
superset parameter list (it adds `resourceGroupName`,
`vnetResourceGroupName`, `vnetName`,
`privateEndpointSubnetAddressPrefix`, `createNetworkSecurityGroup`,
`existingNetworkSecurityGroupId`). **ARM rejects a parameters file
containing keys the target template doesn't declare**, so don't pass that
file directly to `main.bicep` — use inline overrides as above, keeping the
shared values in sync with `deployment/<env>/parameters.json` by hand.

## Post-deployment Portal verification

Check these in order after a deployment. `<env>`/`<instance>` below follow
the naming convention (e.g. `dev`/`001`); substitute your actual values.

**Resource group, subnet, NSG**

- Resource groups → `rg-rsbc-dmer-<env>` — region `Canada Central`; tags
  `project: dmer-automation`, `environment: <env>`, `service: shared`,
  `costCenter`, `owner`, `dataClassification` as set in parameters.json.
- The platform VNet → Subnets → `snet-rsbc-dmer-pe-<env>-<instance>` —
  address range matches `privateEndpointSubnetAddressPrefix`; "Private
  endpoint network policies" = **Disabled** (required for private
  endpoints to be placeable there at all); Network security group =
  `nsg-rsbc-dmer-pe-<env>-<instance>`.
- Network security groups → `nsg-rsbc-dmer-pe-<env>-<instance>` — inbound/
  outbound rules should be empty aside from Azure's implicit defaults
  (unless the platform team's own Azure Policy injects baseline rules into
  any NSG in their VNet, which would be expected, not a problem).

**Managed identity and Document Intelligence**

- Managed Identities → `id-rsbc-dmer-di-processor-<env>-<instance>` → left
  nav "Azure role assignments" — should list two: `Cognitive Services
  User` scoped to the DI account, `Storage Blob Data Reader` scoped to the
  storage account.
- Document Intelligence (Cognitive Services, kind Form Recognizer) →
  `di-rsbc-dmer-shared-<env>-<instance>`:
  - Overview: Endpoint matches the deployment output; pricing tier matches
    `documentIntelligenceSku`.
  - Identity blade: System assigned = On; User assigned shows the managed
    identity above.
  - Networking blade: Public network access = **Disabled** (this account
    is private-only by design, unlike the storage account below — Studio/
    data-plane calls always need a tunnel or VNet route regardless of this
    setting, since it's `Disabled` either way). Private endpoint
    connections tab lists `pe-di-rsbc-dmer-shared-<env>-<instance>`,
    status **Approved** (same-tenant private endpoints created via Bicep
    auto-approve).
  - Access control (IAM): `Cognitive Services User` granted to the managed
    identity.

**Storage account**

- Storage accounts → `stdmer<env><region>-<instance>`:
  - Configuration blade: Minimum TLS version `1.2`; "Allow Blob public
    access" = Disabled; "Allow storage account key access" matches
    `allowSharedKeyAccess`.
  - Networking blade: Public network access = "Enabled from selected
    virtual networks and IP addresses"; Resource instances section lists
    `Microsoft.CognitiveServices/accounts` → that environment's DI account
    name. Private endpoint connections tab lists
    `pe-st-rsbc-dmer-shared-<env>-<instance>`, status **Approved**.
  - Access control (IAM): `Storage Blob Data Reader` granted to the
    managed identity.
  - Containers → `raw` — Public access level **Private (no anonymous
    access)**; empty until training data is uploaded/copied in.
- Private endpoints → both `pe-di-...` and `pe-st-...` — DNS configuration
  tab should show an A-record within ~10 minutes of deployment (see step 7
  above); empty immediately after deployment is expected, not a fault.

## Common errors and troubleshooting

- **`az group exists` reports a resource group as missing when it isn't.**
  If your `az` CLI's default output format is configured as `table`
  (`az configure -l` to check), un-pinned `az group exists` output renders
  as a rendered table instead of the bare string `true`, which breaks
  naive string comparisons. `check_platform_prerequisites.sh` already pins
  `-o tsv` everywhere for this reason; if you're running commands by hand,
  do the same.
- **`what-if`/`create` fails with a subnet address conflict.** The CIDR in
  `privateEndpointSubnetAddressPrefix` overlaps an existing subnet in the
  VNet. Re-run "Choosing a subnet CIDR" above against the current state —
  someone may have added a subnet since you last checked.
- **ARM rejects your parameters file with an "extra/unknown parameter"
  error.** You passed `deployment/<env>/parameters.json` (written for
  `subscription.bicep`'s parameter set) directly to `main.bicep`, which
  declares fewer parameters. Use the inline-override form in "Redeploying
  only the workload" above instead.
- **`check_platform_prerequisites.sh` can't find the Private DNS zones.**
  Expected for this landing zone — see "Private DNS: confirmed behavior"
  below. Leave `privateDnsZoneIdCognitiveServices`/`privateDnsZoneIdBlob`
  empty regardless.
- **A private endpoint doesn't resolve right after deployment.** Give it
  up to ~10 minutes for the platform's DINE policy to register the DNS
  A-record (step 7 above) before troubleshooting further.
- **`document_intelligence_model.py build`/`copy` fails with a 409
  Conflict.** A model with that `--model-id` already exists. The script
  checks for this and skips by default — pass `--force` if you actually
  want to delete and recreate it under the same ID. See
  `scripts/ci/README.md`.
- **RBAC role assignment doesn't appear immediately after deployment.**
  Azure AD role assignment propagation can lag a minute or two behind the
  ARM deployment finishing — refresh the Portal blade before assuming
  something's wrong.

## Rollback

See `docs/deployment/rollback-guide.md` and
`scripts/ci/rollback_infrastructure.sh` — a parameterized, confirm-gated
script that removes exactly what this guide's deployment procedure
creates, for one environment at a time.

## What's automated vs. platform-owned

| Resource | Who creates it |
|---|---|
| Resource group (`rg-rsbc-dmer-<env>`) | **This repo**, via `infrastructure/bicep/subscription.bicep` (`modules/shared/resource-group.bicep`). |
| Private-endpoint subnet inside the existing VNet | **This repo**, via `subscription.bicep` (`modules/networking/subnet.bicep`), deployed scoped to the VNet's own resource group. |
| NSG for that subnet | **This repo**, via `subscription.bicep` (`modules/networking/network-security-group.bicep`), unless you set `createNetworkSecurityGroup: false` and point at a platform-managed one instead. |
| The landing zone VNet itself (`f11861-<env>-vwan-spoke`) | **Platform team.** Never created or modified by this repository. |
| VNet peering to the hub, flow logs, Network Watcher | **Platform team.** |
| Private DNS zones (`privatelink.cognitiveservices.azure.com`, `privatelink.blob.core.windows.net`) and their link to the VNet | **Confirmed platform-owned.** Both zones already exist centrally in the landing zone hub; the platform team's Deploy-If-Not-Exists (DINE) policy creates the DNS A-record automatically after a private endpoint is deployed (observed within ~10 minutes) — see [BC Gov TechDocs, Azure best practices — Be Mindful, "Private Endpoints and DNS"](https://developer.gov.bc.ca/docs/default/component/public-cloud-techdocs/azure/best-practices/be-mindful/#private-endpoints-and-dns). `privateDnsZoneIdCognitiveServices`/`privateDnsZoneIdBlob` stay empty — do not set them (that would fight with the policy, not complement it). See "Private DNS: confirmed behavior" below. |
| Document Intelligence account, Storage account, Managed Identity, RBAC, private endpoints | **This repo**, via `main.bicep` (invoked as a nested module by `subscription.bicep`, or standalone once the RG/subnet exist). |

### Private DNS: confirmed behavior

Confirmed directly by the platform team (and consistent with [BC Gov
TechDocs, Azure best practices — Be Mindful, "Private Endpoints and
DNS"](https://developer.gov.bc.ca/docs/default/component/public-cloud-techdocs/azure/best-practices/be-mindful/#private-endpoints-and-dns)):

- The `privatelink.cognitiveservices.azure.com` and
  `privatelink.blob.core.windows.net` zones already exist centrally in the
  landing zone **hub** subscription — not in this project's subscription,
  and not in the VNet's resource group. Don't expect to find them by
  browsing either. The VNet uses the platform's central DNS resolver,
  which is how resolution reaches those hub-hosted zones without a link
  visible from inside this subscription.
- A **Deploy-If-Not-Exists (DINE) Azure Policy** creates the DNS A-record
  for each private endpoint automatically, after the private endpoint
  itself is deployed. This has been observed to take **up to ~10
  minutes** — a private endpoint that resolves to nothing (or times out)
  immediately after deployment is not a failure, just the policy
  evaluation cycle running. DEV's own deployment confirmed this in
  practice: both private endpoints showed a DNS A-record shortly after
  deployment.
- This repository's Bicep is already written for this: every
  `private-endpoint.bicep` call leaves `privateDnsZoneResourceId` empty
  (equivalent to selecting "No" for "Integrate with private DNS zone" in
  the Portal wizard), and `main.bicep`/`subscription.bicep` leave
  `privateDnsZoneIdCognitiveServices`/`privateDnsZoneIdBlob` empty by
  default.
- **Caveat from the same TechDocs page:** creating more than one private
  endpoint against the same target resource can cause the DINE policy to
  replace an existing A-record, which can break connectivity for the
  other endpoint if one is later deleted. This repository creates exactly
  one private endpoint per resource (Document Intelligence, Storage blob)
  — keep it that way rather than adding a second private endpoint against
  either resource.
- To verify rather than take it on faith: resolve the resource's hostname
  from inside the VNet (e.g. from a Bastion-connected jump box) —
  `nslookup <resource>.privatelink.<suffix>` should return a private IP
  within the private-endpoint subnet once the ~10 minute policy window has
  passed.

## Networking: is the tunnel required?

**No — deploying and configuring the infrastructure in this repository
never requires the tunnel.** The tunnel was only ever a workaround for one
specific problem: a *browser* (Document Intelligence Studio) with no
network route to a private endpoint. Everything Bicep does is an Azure
Resource Manager **control-plane** call to `management.azure.com`, which
is always publicly reachable regardless of the target resource's own
network restrictions. Whether a step needs the tunnel (or an equivalent
private network route) depends entirely on whether it's a control-plane
call or a **data-plane** call to the resource's own endpoint
(`*.cognitiveservices.azure.com`, `*.blob.core.windows.net`).

| Operation | Plane | Tunnel/private network route required? | Why |
|---|---|---|---|
| `az bicep build` / `az bicep lint` | Local compile, no network call | No | Runs entirely on your machine against the Bicep CLI. |
| `az deployment sub what-if` / `az deployment group what-if` | Control plane (ARM) | No | Talks to `management.azure.com`, not the resource's own endpoint. |
| Creating the resource group and the private-endpoint subnet (`subscription.bicep`) | Control plane (ARM) | No | `Microsoft.Resources/resourceGroups` and `Microsoft.Network/virtualNetworks/subnets` are ARM resource creates, same as any other Bicep resource. |
| Creating the Document Intelligence account | Control plane (ARM) | No | `Microsoft.CognitiveServices/accounts` is an ARM resource create/update. |
| Creating private endpoints | Control plane (ARM) | No | `Microsoft.Network/privateEndpoints` is an ARM resource; approval between same-tenant resources is automatic. |
| Storage account + network rule configuration | Control plane (ARM) | No | `networkAcls`, `publicNetworkAccess`, etc. are ARM properties on the storage account resource. |
| Managed identity creation + RBAC role assignments | Control plane (ARM/`Microsoft.Authorization`) | No | Role assignment creation is an ARM call regardless of the target resource's network restrictions. |
| **Document Intelligence Studio** (project creation, labeling, "Analyze") | **Data plane** | **Yes** (or an equivalent private network route) | Studio runs in your browser and calls the account's own `documentintelligence/*` REST surface directly. Once `publicNetworkAccess` is `Disabled`, your browser needs a network path to the private endpoint — a VPN/ExpressRoute route into the VNet, a jump box + Bastion inside the VNet, or (as a stopgap) a tunnel/proxy. |
| **Custom model training/build/copy**, via Studio, REST, SDK, or CLI | **Data plane** | **Yes**, unless the caller is already inside/routed to the VNet | Same reasoning as Studio — `documentModels:build`/`:copyTo` hit the account's own endpoint. A script run from a self-hosted CI runner or automation job deployed *inside* the VNet does not need a tunnel; one run from a laptop outside it does. |
| Blob upload/listing from Studio's "configure data source" step | **Data plane** | **Yes**, if the storage account's own network path is also restricted | Same private-endpoint reasoning applies to Storage as to Document Intelligence. |

**Bottom line:** run `az bicep build/lint`, `az deployment sub what-if`/`az
deployment group what-if`, and `az deployment sub create`/`az deployment
group create` from wherever is convenient — a laptop, Cloud Shell, or a
GitHub Actions runner with standard internet egress — none of them need
the tunnel. Only reserve the tunnel (or, better, a real
VPN/Bastion/self-hosted-runner-in-VNet route) for Studio sessions and
data-plane training/build calls once the account has no public network
access. See "Recommended replacement for ad hoc tunnelling" below.

## Document Intelligence Studio, labeling, and custom models: what Bicep can and can't do

- **Bicep creates the account** (`infrastructure/bicep/modules/ai/document-intelligence.bicep`)
  — that's it. There is no ARM resource type for a Studio "project," a
  labeling configuration, or a trained/custom model; these are data-plane
  concepts that live as files (`fields.json`, `<doc>.ocr.json`,
  `<doc>.labels.json`) in your blob container plus server-side state on
  the Document Intelligence account, not as resources under your
  subscription/resource group.
- **Labeling is inherently manual.** Drawing bounding boxes and assigning
  field names to training documents is an interactive, visual task.
  There's no meaningful way to automate it — Document Intelligence Studio
  (or an equivalent custom labeling UI you build) is the right tool, not
  Bicep or a script.
- **Building (training) and copying a model are the two data-plane
  operations worth automating** once labeling is done — see
  `scripts/ci/document_intelligence_model.py`, which wraps the
  `azure-ai-documentintelligence` SDK's `begin_build_document_model` and
  `begin_copy_model_to`. Prefer **copying** a model that was
  trained/validated in DEV to TEST/PROD over retraining per environment,
  so the same model (and its accuracy characteristics) is what gets
  promoted, consistent with this repo's GitOps promotion model
  (`docs/architecture/repository-design.md` §14). `copy` can optionally
  also carry the labelled training data over via `--copy-training-data` —
  see the script's own docstring.
- Both operations authenticate with `DefaultAzureCredential` (Azure AD),
  matching `docs/standards/security-guidelines.md` — no subscription key.
  The identity used needs the `Cognitive Services User` role on the
  target account (already granted to
  `id-rsbc-dmer-di-processor-<env>-001` by `main.bicep`; use an
  equivalent identity, e.g. an `az login` user or a federated OIDC
  identity, with the same role for interactive/CI runs). For `copy`, that
  role is needed on **both** the source and target accounts.
- Both `build` and `copy` are idempotent by default: they check whether a
  model with the given `--model-id` already exists before doing anything,
  and skip rather than fail with a 409 if it does. Pass `--force` to
  delete and recreate.

### Recommended replacement for ad hoc tunnelling

The Firefox tunnel got Studio working, but it's a manual, per-session
workaround. Options, in order of how much they reduce recurring manual
work (confirm feasibility with the platform team):

1. **VPN/ExpressRoute into the landing zone**, if the platform team offers
   one — gives any BC Gov-managed device a normal network route to the
   private endpoint; Studio and any local tooling then just work, no proxy
   needed.
2. **A jump box (VM) + Azure Bastion inside the platform VNet** — run
   Studio's browser session from the jump box.
3. **Run `scripts/ci/document_intelligence_model.py` from inside the
   VNet** (a self-hosted GitHub Actions runner, an Azure Automation
   Runbook, or a one-off Container App job deployed into the spoke) for
   the build/copy steps specifically — removes the human from the network
   path entirely for anything past labeling.
4. Continue using the tunnel only for the labeling step in Studio, since
   that step is irreducibly manual and interactive.

## Deploying services

Each service is built independently (Docker image for Container Apps, zip
package for Function Apps) and deployed via its stage in the corresponding
`.github/workflows/deploy-*.yml` pipeline. See each service's `README.md`
for its build command. (Not yet applicable to this deployment — no compute
services are wired into `main.bicep` yet.)
