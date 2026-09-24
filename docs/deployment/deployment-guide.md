
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
  account + blob containers, both private endpoints, the RBAC role
  assignments connecting them, and — only when `containerAppsEnvironmentId`
  is supplied — the di-processor Container App (see
  [di-processor Container App](#di-processor-container-app) below).

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
  ├─ stdmer<env><region><instance>                    (Storage Account, e.g. stdmerdevcac001)
  │    ├─ raw                                          (blob container — source/training PDFs)
  │    ├─ extracted-dmer                               (blob container — di-processor output)
  │    └─ pe-st-rsbc-dmer-shared-<env>-<instance>     (its private endpoint)
  ├─ 3 RBAC role assignments, granted to the Managed Identity:
  │    Cognitive Services User (DI account), Storage Blob Data Reader (raw
  │    container), Storage Blob Data Contributor (extracted-dmer container)
  └─ ca-rsbc-dmer-di-processor-<env>-<instance>       (Container App — only when
                                                       containerAppsEnvironmentId is set)
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
| `blobContainerNames` | `["raw", "extracted-dmer"]` | same | same | Both are required: `main.bicep` grants di-processor container-scoped roles on them and fails if either is missing |
| `allowSharedKeyAccess` | `true` | `false` | `false` | DEV allows account-key auth for convenience; TEST/PROD require Azure AD only |
| `disableLocalAuthDocumentIntelligence` | `false` | `false` | `true` | PROD disables API-key auth on the DI account entirely |
| `costCenter` / `owner` / `dataClassification` | `RSBC` / `RSBC-DMER` / `protected-b` | same | same | Tag values — change if these aren't accurate for your deployment |

The di-processor Container App parameters are listed separately under
[di-processor Container App](#di-processor-container-app). They are
all inert while `containerAppsEnvironmentId` is empty (the default), which
is the current state of every environment.

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

Expect roughly 13 resources, all `+ Create`: the application resource
group, NSG, subnet, managed identity, Document Intelligence account, its
private endpoint, storage account, 2 blob containers, its private endpoint,
and 3 role assignments — plus the di-processor Container App (14) when
`containerAppsEnvironmentId` is set. Anything else — especially a `~ Modify` or
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
  --parameters blobContainerNames='["raw", "extracted-dmer"]' \
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
When the Container App is enabled, add its parameters (listed under
[di-processor Container App](#di-processor-container-app)) as further
`--parameters` overrides.

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
  nav "Azure role assignments" — should list three: `Cognitive Services
  User` scoped to the DI account, `Storage Blob Data Reader` scoped to the
  `raw` container, `Storage Blob Data Contributor` scoped to the
  `extracted-dmer` container. (Roles on shared resources — Service Bus, Key
  Vault, App Configuration, PostgreSQL — are granted by their own
  workstreams; see [di-processor Container App](#di-processor-container-app).)
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

- Storage accounts → `stdmer<env><region><instance>` (e.g. `stdmerdevcac001`):
  - Configuration blade: Minimum TLS version `1.2`; "Allow Blob public
    access" = Disabled; "Allow storage account key access" matches
    `allowSharedKeyAccess`.
  - Networking blade: Public network access = "Enabled from selected
    virtual networks and IP addresses"; Resource instances section lists
    `Microsoft.CognitiveServices/accounts` → that environment's DI account
    name. Private endpoint connections tab lists
    `pe-st-rsbc-dmer-shared-<env>-<instance>`, status **Approved**.
  - Access control (IAM): roles are container-scoped, so check them on
    each container, not the account — `raw` → `Storage Blob Data Reader`,
    `extracted-dmer` → `Storage Blob Data Contributor`, both granted to the
    managed identity.
  - Containers → `raw` and `extracted-dmer` — Public access level **Private
    (no anonymous access)**. `raw` is empty until training data is
    uploaded/copied in; `extracted-dmer` fills with
    `<document_id>/{top_level,ocr,handwritten,combined}.json` once
    di-processor runs.
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
| di-processor Container App | **This repo**, via `main.bicep` (`modules/compute/container-app.bicep`) — only when `containerAppsEnvironmentId` is supplied. |
| Container Apps Environment, Service Bus namespace + queues, PostgreSQL, App Configuration, Key Vault (and the OpenAI key secret), Log Analytics, container registry | **Other workstreams.** Passed in by ID / FQDN / URI; di-processor's access to them is granted there — see [di-processor Container App](#di-processor-container-app). |
| Azure OpenAI deployment | **External** — separate AI Hub subscription; reached by endpoint + API key. |

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
for its build command. The deploy pipelines are still placeholders, so the
one compute service wired into `main.bicep` today — di-processor — is
deployed with the steps below.

## di-processor Container App

The extraction stage (see `docs/development/stages/02-extraction.md`). One
Container App, `ca-rsbc-dmer-di-processor-<env>-<instance>`, running as the
`id-rsbc-dmer-di-processor-<env>-<instance>` user-assigned identity, scaled
by KEDA on `dmer-raw` queue depth (`minReplicas` 1 in PROD, 0 elsewhere).
Ingress is **internal only** — it exists solely for the platform's
`/healthz` (liveness) and `/readyz` (readiness) probes on port 8080.

It is **not deployed** until `containerAppsEnvironmentId` is set in
`deployment/<env>/parameters.json`. Every environment currently has it
empty, with `__PLACEHOLDER__` values for the rest.

### Parameters

| Parameter | Becomes | Where it comes from |
|---|---|---|
| `containerAppsEnvironmentId` | (enables the app) | Container Apps Environment resource ID — Container Apps workstream |
| `diProcessorImage` | container image | Your build, e.g. `<registry>.azurecr.io/di-processor:<tag>` (see [Build and push](#build-and-push-the-image)) |
| `containerRegistryServer` | registry pull via the identity | Registry login server; empty = public image |
| `appConfigurationEndpoint` | `APP_CONFIGURATION_ENDPOINT` | App Configuration workstream |
| `diCustomModelId` | `DI_CUSTOM_MODEL_ID` | The trained custom model id (`rsbc-ocr-dmer-v9` as of this writing — confirm per environment) |
| `llmPromptVersion` | `LLM_PROMPT_VERSION` (only set when non-empty) | Optional; empty records `prompt=unversioned` on `dmer_stage_run.model_version` |
| `openAiEndpoint` / `openAiDeployment` / `openAiApiVersion` | `AZURE_OPENAI_ENDPOINT` / `_DEPLOYMENT` / `_API_VERSION` | The external AI Hub deployment |
| `openAiApiKeySecretUri` | `AZURE_OPENAI_API_KEY`, as a Key Vault **secret reference** (never a plain value) | Key Vault secret URI — Key Vault workstream |
| `logAnalyticsWorkspaceId` | diagnostics | Optional; shared workspace resource ID |

Set automatically by `main.bicep` from the resources it creates, not
parameters: `BLOB_ACCOUNT_URL` (storage account), `DOC_INTELLIGENCE_ENDPOINT`
(DI account), `SERVICE_BUS_NAMESPACE_FQDN` (the Service Bus namespace — also
the KEDA scaler's namespace), `POSTGRES_HOST` and `POSTGRES_DATABASE` (the
PostgreSQL flexible server and its `dmer` database), and `AZURE_CLIENT_ID` (the
identity's client ID — required so `DefaultAzureCredential` picks the
user-assigned identity). Queue,
container, and port names use the code defaults (`dmer-raw`,
`dmer-extracted`, `extracted-dmer`, `8080`).

Bicep does **not** check that the required values are filled in once
`containerAppsEnvironmentId` is set. A missing or `__PLACEHOLDER__` value
shows up only at runtime — the app exits at startup on a missing setting,
or fails its first call to the resource it names.

### Access the identity needs on shared resources

`main.bicep` grants the roles on resources it creates: Cognitive Services
User on DI, Blob Data Reader on `raw`, Blob Data Contributor on
`extracted-dmer`, **Service Bus Data Receiver on `dmer-raw`** and **Service Bus
Data Sender on `dmer-extracted`** (all queue/container-scoped). For the rest,
ask the owning workstream to grant
`id-rsbc-dmer-di-processor-<env>-<instance>`:

| Resource | Role | Without it |
|---|---|---|
| Key Vault holding the OpenAI key | Key Vault Secrets User | The Container App cannot resolve the secret reference; the revision fails to start |
| App Configuration | App Configuration Data Reader | Configuration reads fail |
| PostgreSQL (`dmer` database) | An Entra ID database user for the identity | Every status write fails (`DB_WRITE_FAILED`). **Note:** the app does not yet acquire a Managed Identity token for PostgreSQL — deferred; see the di-processor spec |
| Container registry (if `containerRegistryServer` is set) | AcrPull | Image pull fails |

### Build and push the image

The Dockerfile needs the **repo root** as build context (it copies
`libs/dmer_common`):

```bash
docker build -f services/di-processor/Dockerfile -t <registry>.azurecr.io/di-processor:<tag> .
az acr login --name <registry>
docker push <registry>.azurecr.io/di-processor:<tag>
```

### Enable and deploy

1. Confirm the shared resources exist and the roles above are granted.
2. In `deployment/<env>/parameters.json`, set `containerAppsEnvironmentId`
   and replace every `__PLACEHOLDER__` in the di-processor block.
3. Run the normal `what-if` / `create` (steps 5–6 above). The plan should
   add one `Microsoft.App/containerApps` resource; on later image-only
   changes, expect a `~ Modify` of that resource alone.

### Verify

- Container Apps → `ca-rsbc-dmer-di-processor-<env>-<instance>`:
  - Revisions: the latest revision is **Running** and healthy
    (liveness/readiness probes passing).
  - Identity: user assigned = `id-rsbc-dmer-di-processor-<env>-<instance>`.
  - Secrets blade — the OpenAI key entry is listed as a **Key Vault
    reference** (never a stored value).
  - Scale: rule on `dmer-raw`; replica count 0 at idle outside PROD.
- Log stream / Log Analytics: `di-processor started; consuming` on startup.
  A failed document logs `pipeline failed` with an `error_code`
  (`docs/development/stages/02-extraction.md` §Failure codes); the same code
  is the dead-letter reason on `dmer-raw`, and on `dmer_stage_run.error_code`.
