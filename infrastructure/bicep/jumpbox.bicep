// jumpbox.bicep
//
// Resource-group-scoped entry point for the debug/access jump box:
// rsbc-ai-jumpbox (VM + desktop session + sb_queue_viewer.py, see
// modules/compute/jumpbox-vm.bicep and scripts/deployment/jumpbox-setup.sh)
// and rsbc-dmer-ai-bastion (the only network path to it, see
// modules/networking/bastion-host.bicep). Targets
// rsbc-dmer-ai-optimization-rg, the same manually-created resource group
// intake-processor.bicep already deploys into — a separate entry point
// from main.bicep for the same reason intake-processor.bicep is one (see
// that file's header): these resources already live there, provisioned by
// hand before this repo's Bicep existed.
//
// This template is written to capture what this VM was built to do, so a
// fresh jump box can be reproduced from the repo alone instead of redoing
// all of this by hand again -- it does not attempt to adopt/reconcile the
// specific already-existing manually-created VM and Bastion host (Bicep
// has no simple "import" for that). See
// docs/deployment/jumpbox-setup.md for the full usage guide, the known
// xrdp lag/TLS issues discovered while using this VM, and how to switch
// between XFCE and GNOME.
//
// Validate with `what-if` before trusting this against a live resource
// group -- same methodology as docs/deployment/deployment-guide.md.

targetScope = 'resourceGroup'

import { buildTags } from 'modules/shared/tags.bicep'

@description('Target environment: dev | test | prod')
@allowed([
  'dev'
  'test'
  'prod'
])
param environment string

@description('Azure region. BC Gov landing zone workloads are Canada-only for data residency — see docs/standards/bc-gov-alignment.md.')
@allowed([
  'canadacentral'
  'canadaeast'
])
param location string = 'canadacentral'

@description('Jump box VM name.')
param vmName string = 'rsbc-ai-jumpbox'

@description('Bastion host name.')
param bastionName string = 'rsbc-dmer-ai-bastion'

@description('VM size — see jumpbox-vm.bicep\'s param for the performance caveats discovered on the default.')
param vmSize string = 'Standard_DC2s_v3'

@description('Resource ID of the existing subnet the VM\'s NIC attaches to (not AzureBastionSubnet — that\'s created separately below).')
@minLength(1)
param vmSubnetId string

@description('Name of the resource group containing the existing platform VNet, e.g. f11861-dev-networking.')
@minLength(1)
param vnetResourceGroupName string

@description('Name of the existing platform-provided VNet, e.g. f11861-dev-vwan-spoke.')
@minLength(1)
param vnetName string

@description('Address prefix for AzureBastionSubnet (min /26) — verify it doesn\'t overlap an existing subnet first, same as any other subnet in this VNet.')
@minLength(1)
param bastionSubnetAddressPrefix string

@description('Local admin username for the VM — also the RDP login user.')
@minLength(1)
param adminUsername string

@secure()
@description('Local admin password for the VM. Supply via a pipeline secret, never a parameters.json file.')
param adminPassword string

@description('Name of the existing Service Bus namespace to grant this VM\'s managed identity data-plane read access to (so sb_queue_viewer.py\'s ManagedIdentityCredential auth works).')
@minLength(1)
param serviceBusNamespaceName string

@description('Name of the resource group containing that Service Bus namespace, e.g. rg-rsbc-dmer-dev.')
@minLength(1)
param serviceBusResourceGroupName string

@description('Raw URL to fetch scripts/debug/sb_queue_viewer.py from — see jumpbox-setup.sh\'s header for why this isn\'t pinned to a release/commit.')
param queueViewerRawUrl string = 'https://raw.githubusercontent.com/bcgov/rsbc-ai-dmer-automation/main/scripts/debug/sb_queue_viewer.py'

@description('Raw URL to fetch scripts/deployment/jumpbox-setup.sh from.')
param setupScriptRawUrl string = 'https://raw.githubusercontent.com/bcgov/rsbc-ai-dmer-automation/main/scripts/deployment/jumpbox-setup.sh'

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value.')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'jumpbox', costCenter, owner, dataClassification)

// Azure Service Bus Data Receiver — lets sb_queue_viewer.py peek/receive/
// complete/abandon/dead-letter messages via the VM's own managed identity,
// with zero interactive login. See docs/contracts/queues/ for what this
// tool inspects.
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

// ---------------------------------------------------------------------------
// 1. Bastion — the only network path to the VM below.
// ---------------------------------------------------------------------------
module bastion 'modules/networking/bastion-host.bicep' = {
  name: '${deployment().name}-bastion'
  params: {
    name: bastionName
    location: location
    tags: serviceTags
    vnetResourceGroupName: vnetResourceGroupName
    vnetName: vnetName
    bastionSubnetAddressPrefix: bastionSubnetAddressPrefix
  }
}

// ---------------------------------------------------------------------------
// 2. The jump box VM itself.
// ---------------------------------------------------------------------------
module jumpboxVm 'modules/compute/jumpbox-vm.bicep' = {
  name: '${deployment().name}-vm'
  params: {
    name: vmName
    location: location
    tags: serviceTags
    vmSize: vmSize
    subnetId: vmSubnetId
    adminUsername: adminUsername
    adminPassword: adminPassword
    queueViewerRawUrl: queueViewerRawUrl
    setupScriptRawUrl: setupScriptRawUrl
  }
  dependsOn: [
    bastion
  ]
}

// ---------------------------------------------------------------------------
// 3. Grant the VM's managed identity Service Bus data-plane read access.
//    A separate module, not a raw resource — the namespace lives in its
//    own resource group, which may differ from this deployment's target
//    (see main.bicep's RBAC assignments for the same cross-resource-group
//    pattern, and data-plane-role-assignment.bicep's header for why this
//    can't be a resource declared directly in this file).
// ---------------------------------------------------------------------------
module serviceBusRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    principalId: jumpboxVm.outputs.principalId
    roleDefinitionId: serviceBusDataReceiverRoleId
  }
}

@description('Name of the deployed VM.')
output vmName string = jumpboxVm.outputs.name

@description('Name of the deployed Bastion host.')
output bastionName string = bastion.outputs.name
