# Environment Setup

## Prerequisites

- **Automated by this repository's Bicep**, no manual step needed: resource groups
  (`rg-rsbc-dmer-dev`, `rg-rsbc-dmer-test`, `rg-rsbc-dmer-prod`) and the private-endpoint
  subnet (+ NSG) inside the platform VNet — see `infrastructure/bicep/subscription.bicep` and
  `docs/deployment/deployment-guide.md`.
- **Platform-team-owned, not created here:** the landing zone VNet itself
  (`f11861-<env>-vwan-spoke`), its peering to the hub, flow logs, and Network Watcher — see
  "Platform-team networking prerequisites" below for what this repository still needs
  confirmed/from them before its first deployment to a given environment.
- Azure subscription + RBAC role assignments for the deploying identity: Contributor at
  subscription level (to create the resource group) and Contributor/Network Contributor on the
  resource group containing the platform VNet (to create the subnet there,
  `Microsoft.Network/virtualNetworks/subnets/join/action` +
  `Microsoft.Network/virtualNetworks/subnets/write` at minimum) — **don't assume either is
  already granted; check first**, see "How to check whether prerequisites already exist" below.
- Document Intelligence Studio project, labeling, and custom model training/publishing — see
  `docs/deployment/deployment-guide.md` for exactly what this covers and why it can't be
  automated with Bicep.

## Platform-team networking prerequisites

`infrastructure/bicep/subscription.bicep` reuses the existing platform VNet
(`f11861-dev-vwan-spoke` in DEV) — it does not create a VNet, and it must not. It does create
a subnet inside that VNet (see `infrastructure/bicep/modules/networking/subnet.bicep`) and an
NSG for it (optional — `createNetworkSecurityGroup` parameter). Before the first deployment to
a given environment, confirm with the platform team:

1. **TEST/PROD VNet names.** Only DEV's (`f11861-dev-vwan-spoke`) is confirmed;
   `deployment/test/parameters.json` and `deployment/prod/parameters.json` currently hold an
   unconfirmed guess following the same naming pattern.
2. ~~Whether `privatelink.cognitiveservices.azure.com` and `privatelink.blob.core.windows.net`
   Private DNS zones are already centrally managed~~ — **confirmed by the platform team, no
   longer open.** Both zones already exist centrally in the landing zone hub, and their
   Deploy-If-Not-Exists (DINE) policy creates each private endpoint's DNS A-record
   automatically (observed within ~10 minutes of deployment) — see
   [BC Gov TechDocs, Azure best practices — Be Mindful, "Private Endpoints and DNS"](https://developer.gov.bc.ca/docs/default/component/public-cloud-techdocs/azure/best-practices/be-mindful/#private-endpoints-and-dns)
   and `docs/deployment/deployment-guide.md`, "Private DNS: confirmed behavior". Leave
   `privateDnsZoneIdCognitiveServices`/`privateDnsZoneIdBlob` empty — do not supply zone IDs,
   that would fight the policy rather than help it. One caveat carried over from that TechDocs
   page: don't create more than one private endpoint per resource, or the DINE policy can
   replace an existing A-record and break the other endpoint if one is later deleted.
3. Whether the deploying identity already has subnet-creation rights on the VNet's resource
   group (see above).
4. Whether the platform team mandates a specific NSG configuration on subnets in their VNet
   (e.g. via Azure Policy) rather than allowing an app team to bring their own — if so, set
   `createNetworkSecurityGroup: false` and supply `existingNetworkSecurityGroupId`.
5. What network path (VPN/ExpressRoute, or a jump box + Bastion) is available for
   Document Intelligence Studio sessions and data-plane model build/copy scripts, now that the
   Document Intelligence account has `publicNetworkAccess: Disabled` — see the tunnelling
   analysis in `docs/deployment/deployment-guide.md`. **Do not assume BC Government's specific
   landing zone policy here** — this repository's docs flag it for verification rather than
   guessing, since it varies by landing zone and wasn't confirmed against platform-team
   documentation.

## How to check whether prerequisites already exist

Run `scripts/ci/check_platform_prerequisites.sh` — a read-only script (no changes made) that
checks items 1 and 3 above (resource group existence, VNet/subnet details, and a best-effort
RBAC read) directly against Azure, plus a best-effort search for the Private DNS zones (item 2,
now confirmed resolved — see above; the search will typically come back empty since the zones
live in a hub subscription you likely don't have Reader on, which is expected, not an error).
See `docs/deployment/deployment-guide.md`, "How to check what already exists", for usage and
exact `az` commands if you'd rather run them individually.

None of this blocks running `az bicep build`/`lint`, `az deployment sub what-if`, or the
checker script itself (all control plane/read-only) — only the actual
`az deployment sub create` (which creates the resource group and places a subnet in the
platform's VNet) and any Studio/data-plane work need items 1–5 answered first.

## Per-environment configuration

Environment-specific values (SKUs, scaling limits, allowed IPs, the VNet/subnet references)
live in `deployment/<env>/parameters.json`. Never hardcode environment values in
`infrastructure/bicep/modules/*`.

## Secrets

All secrets are stored in Key Vault and referenced by App Configuration / service
Managed Identities. No secret is ever committed to this repository. (No Key Vault module is
deployed yet — this applies once one is added; today, the Document Intelligence + Storage
slice deployed by `main.bicep` uses Managed Identity/RBAC exclusively, no secrets at all.)

### External Azure OpenAI endpoint

`normalizer-service` calls a model hosted in a separate Azure AI Hub/AI Foundry project, in
a separate Azure subscription owned by another team — the model is already deployed there;
we do not provision or manage it. Per environment, a Key Vault secret holds the endpoint URL
and API key for that project's OpenAI resource, and `normalizer-service` reads it via
`libs/dmer_common/config`. This is the one intentional exception to "no secrets, Managed
Identity everywhere" — see `docs/architecture/repository-design.md` §10 and §13 (item 8).
