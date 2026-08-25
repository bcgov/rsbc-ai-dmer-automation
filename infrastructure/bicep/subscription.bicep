// subscription.bicep
//
// Subscription-scoped entry point. Creates what main.bicep (resourceGroup
// scope) cannot: the application resource group itself, and the
// private-endpoint subnet (+ NSG) inside the EXISTING platform-provided
// VNet. Then deploys main.bicep's workload (Managed Identity, Document
// Intelligence, Storage) into the newly created resource group.
//
// Use this for a fresh environment (nothing exists yet). If the resource
// group and subnet already exist (e.g. re-running just the workload after
// initial provisioning), deploy main.bicep directly at resourceGroup scope
// instead — see docs/deployment/deployment-guide.md.
//
// Still platform-team-owned, not created here: the VNet itself
// (f11861-<env>-vwan-spoke), its peering to the hub, flow logs, and
// Network Watcher. See docs/deployment/environment-setup.md for how to
// confirm you have the rights (subnet-join + write) to create a subnet
// inside their VNet before running this template.

targetScope = 'subscription'

import { resourceName } from 'modules/shared/naming.bicep'
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

@description('Zero-padded instance counter, e.g. "001". See docs/standards/naming-conventions.md.')
param instance string = '001'

@description('Name of the application resource group to create, e.g. rg-rsbc-dmer-dev.')
param resourceGroupName string

@description('Name of the resource group that contains the existing platform-provided VNet. Confirm with the platform team — see docs/deployment/environment-setup.md.')
param vnetResourceGroupName string

@description('Name of the existing platform-provided VNet, e.g. f11861-dev-vwan-spoke.')
param vnetName string

@description('Address prefix for the new private-endpoint subnet, e.g. 10.x.x.x/27. Must not overlap an existing subnet in the VNet — verify with `az network vnet subnet list` first (see docs/deployment/environment-setup.md). No safe default exists; this is deliberately required with no fallback.')
@minLength(1)
param privateEndpointSubnetAddressPrefix string

@description('Whether to create a new NSG for the private-endpoint subnet. Set false and supply existingNetworkSecurityGroupId if the platform team requires you to use one they manage instead.')
param createNetworkSecurityGroup bool = true

@description('Resource ID of an existing NSG to use instead of creating one. Only used when createNetworkSecurityGroup is false.')
param existingNetworkSecurityGroupId string = ''

@description('Optional resource ID of the platform/hub-managed Private DNS Zone for privatelink.cognitiveservices.azure.com. Leave empty if the platform team registers private endpoints in their zone(s) out of band instead — see docs/deployment/environment-setup.md.')
param privateDnsZoneIdCognitiveServices string = ''

@description('Optional resource ID of the platform/hub-managed Private DNS Zone for privatelink.blob.core.windows.net. Leave empty if the platform team registers private endpoints in their zone(s) out of band instead.')
param privateDnsZoneIdBlob string = ''

@description('Document Intelligence pricing tier.')
param documentIntelligenceSku string = 'S0'

@description('Storage account SKU — Standard_LRS in dev, consider Standard_ZRS or Standard_GRS in test/prod via parameters.json.')
param storageSkuName string = 'Standard_LRS'

@description('Blob containers to create on the storage account.')
param blobContainerNames array = [
  'raw'
]

@description('Whether Storage Account Shared Key access is allowed. Prefer false once all consumers use Azure AD/Managed Identity.')
param allowSharedKeyAccess bool = true

@description('Whether to disable Document Intelligence API-key auth in favour of Azure AD/RBAC only.')
param disableLocalAuthDocumentIntelligence bool = false

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var sharedTags = buildTags(environment, 'shared', costCenter, owner, dataClassification)
var privateEndpointSubnetName = resourceName('snet', 'pe', environment, instance)
var networkSecurityGroupName = resourceName('nsg', 'pe', environment, instance)

// ---------------------------------------------------------------------------
// 1. Application resource group
// ---------------------------------------------------------------------------
module appResourceGroup 'modules/shared/resource-group.bicep' = {
  name: '${deployment().name}-rg'
  params: {
    name: resourceGroupName
    location: location
    tags: sharedTags
  }
}

// ---------------------------------------------------------------------------
// 2. NSG + subnet inside the existing platform VNet (deployed scoped to the
//    VNet's own resource group, which the platform team owns)
// ---------------------------------------------------------------------------
module nsg 'modules/networking/network-security-group.bicep' = if (createNetworkSecurityGroup) {
  name: '${deployment().name}-nsg'
  scope: resourceGroup(vnetResourceGroupName)
  params: {
    name: networkSecurityGroupName
    location: location
    tags: sharedTags
  }
}

var networkSecurityGroupId = createNetworkSecurityGroup ? (nsg.?outputs.?id ?? '') : existingNetworkSecurityGroupId

module subnet 'modules/networking/subnet.bicep' = {
  name: '${deployment().name}-subnet'
  scope: resourceGroup(vnetResourceGroupName)
  params: {
    vnetName: vnetName
    name: privateEndpointSubnetName
    addressPrefix: privateEndpointSubnetAddressPrefix
    networkSecurityGroupId: networkSecurityGroupId
  }
}

// ---------------------------------------------------------------------------
// 3. Workload (Managed Identity, Document Intelligence, Storage) — see
//    main.bicep. Deployed scoped to the resource group created in step 1.
// ---------------------------------------------------------------------------
module workload 'main.bicep' = {
  name: '${deployment().name}-workload'
  scope: resourceGroup(resourceGroupName)
  params: {
    environment: environment
    location: location
    instance: instance
    privateEndpointSubnetId: subnet.outputs.id
    privateDnsZoneIdCognitiveServices: privateDnsZoneIdCognitiveServices
    privateDnsZoneIdBlob: privateDnsZoneIdBlob
    documentIntelligenceSku: documentIntelligenceSku
    storageSkuName: storageSkuName
    blobContainerNames: blobContainerNames
    allowSharedKeyAccess: allowSharedKeyAccess
    disableLocalAuthDocumentIntelligence: disableLocalAuthDocumentIntelligence
    costCenter: costCenter
    owner: owner
    dataClassification: dataClassification
  }
  dependsOn: [
    appResourceGroup
  ]
}

@description('Name of the created resource group.')
output resourceGroupName string = appResourceGroup.outputs.name

@description('Resource ID of the created private-endpoint subnet.')
output privateEndpointSubnetId string = subnet.outputs.id

@description('Document Intelligence account name.')
output documentIntelligenceName string = workload.outputs.documentIntelligenceName

@description('Document Intelligence data-plane endpoint.')
output documentIntelligenceEndpoint string = workload.outputs.documentIntelligenceEndpoint

@description('Storage account name.')
output storageAccountDeployedName string = workload.outputs.storageAccountDeployedName
