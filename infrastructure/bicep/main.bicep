// main.bicep
//
// Top-level orchestration template. Composes the modules under ./modules into
// the DMER Automation environment.
//
// Scope of this deployment: the Document Intelligence + Storage Account
// slice (Managed Identity, private endpoints, RBAC, network configuration)
// that DEV was manually provisioned with. This template is resource-group
// scoped and expects the resource group and private-endpoint subnet to
// already exist — see subscription.bicep (subscription-scoped), which
// creates both and then invokes this template as a nested module. Deploy
// this file directly (not via subscription.bicep) only once those already
// exist.
//
// Still genuinely manual/platform-owned, not created anywhere in this
// repository: the Document Intelligence Studio project/labeling
// configuration/custom model training (data-plane, not an ARM resource —
// see docs/deployment/deployment-guide.md), the landing zone/subscription,
// and the VNet itself/its peering/flow logs/Network Watcher (platform
// team). See docs/deployment/environment-setup.md for the full list.
//
// The remaining pipeline modules (Service Bus, PostgreSQL, Key Vault, App
// Configuration, Container Apps, Function Apps, Monitor) are intentionally
// not wired in yet — see docs/architecture/repository-design.md §11 for the
// full dependency diagram and eventual module order. Add them here,
// following the same pattern (module -> RBAC role assignment in this file),
// as each service is built.
//
// NOTE: Azure OpenAI is NOT provisioned here. normalizer-service consumes a
// model hosted in a separate Azure AI Hub/AI Foundry project in a separate
// subscription via an endpoint URL + API key stored in Key Vault — see
// docs/architecture/repository-design.md §10 and §13 (item 8). Not relevant
// to this deployment (no Key Vault or normalizer-service yet either).
//
// Deployed per-environment with deployment/<env>/parameters.json — see
// docs/deployment/deployment-guide.md.

targetScope = 'resourceGroup'

import { resourceName, storageAccountName } from 'modules/shared/naming.bicep'
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

@description('Resource ID of the existing platform-provided subnet in f11861-dev-vwan-spoke (or the test/prod equivalent) that private endpoints are placed in. Must be supplied by the platform team — see docs/deployment/environment-setup.md. Deployment fails fast (empty subnet ID) if not supplied.')
@minLength(1)
param privateEndpointSubnetId string

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
var documentIntelligenceAccountName = resourceName('di', 'shared', environment, instance)
var storageAccountNameValue = storageAccountName(environment, location, instance)
var diProcessorIdentityName = resourceName('id', 'di-processor', environment, instance)

// ---------------------------------------------------------------------------
// 1. Managed Identity
//
// One identity for di-processor (the documented consumer of both Document
// Intelligence and the training/source storage container — see
// docs/architecture/repository-design.md §3, §11). Additional per-service
// identities are added here as their compute modules are built.
// ---------------------------------------------------------------------------
module diProcessorIdentity 'modules/identity/managed-identity.bicep' = {
  name: '${deployment().name}-id-di-processor'
  params: {
    name: diProcessorIdentityName
    location: location
    tags: sharedTags
  }
}

// ---------------------------------------------------------------------------
// 2. Document Intelligence
// ---------------------------------------------------------------------------
module documentIntelligence 'modules/ai/document-intelligence.bicep' = {
  name: '${deployment().name}-docint'
  params: {
    name: documentIntelligenceAccountName
    location: location
    tags: sharedTags
    skuName: documentIntelligenceSku
    userAssignedIdentityId: diProcessorIdentity.outputs.id
    privateEndpointSubnetId: privateEndpointSubnetId
    privateDnsZoneResourceIdCognitiveServices: privateDnsZoneIdCognitiveServices
    privateEndpointName: resourceName('pe-di', 'shared', environment, instance)
    disableLocalAuth: disableLocalAuthDocumentIntelligence
  }
}

// ---------------------------------------------------------------------------
// 3. Storage Account (+ blob containers)
//
// trustedResourceIds grants the Document Intelligence account's
// system-assigned identity firewall access to blobs — see the network model
// note at the top of modules/storage/storage-account.bicep.
// ---------------------------------------------------------------------------
module storage 'modules/storage/storage-account.bicep' = {
  name: '${deployment().name}-storage'
  params: {
    name: storageAccountNameValue
    location: location
    tags: sharedTags
    skuName: storageSkuName
    trustedResourceIds: [
      documentIntelligence.outputs.id
    ]
    privateEndpointSubnetId: privateEndpointSubnetId
    privateDnsZoneResourceIdBlob: privateDnsZoneIdBlob
    privateEndpointName: resourceName('pe-st', 'shared', environment, instance)
    allowSharedKeyAccess: allowSharedKeyAccess
  }
}

module blobContainers 'modules/storage/blob-containers.bicep' = {
  name: '${deployment().name}-blob-containers'
  params: {
    storageAccountName: storage.outputs.name
    containerNames: blobContainerNames
  }
}

// ---------------------------------------------------------------------------
// 4. RBAC role assignments (least privilege only — see
//    docs/standards/security-guidelines.md)
// ---------------------------------------------------------------------------
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'

resource documentIntelligenceExisting 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: documentIntelligenceAccountName
  dependsOn: [
    documentIntelligence
  ]
}

resource storageAccountExisting 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountNameValue
  dependsOn: [
    storage
  ]
}

@description('Lets id-rsbc-dmer-di-processor call the Document Intelligence data-plane API (analyze/build/labeling).')
resource diProcessorCognitiveServicesUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(documentIntelligenceExisting.id, diProcessorIdentityName, cognitiveServicesUserRoleId)
  scope: documentIntelligenceExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUserRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Lets id-rsbc-dmer-di-processor read blobs (training/source documents). Read-only — di-processor writes go through libs/dmer_common/storage once that service is built, at which point revisit for Contributor if needed.')
resource diProcessorStorageBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storageAccountExisting.id, diProcessorIdentityName, storageBlobDataReaderRoleId)
  scope: storageAccountExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output documentIntelligenceName string = documentIntelligence.outputs.name
output documentIntelligenceEndpoint string = documentIntelligence.outputs.endpoint
output storageAccountDeployedName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint
output diProcessorManagedIdentityClientId string = diProcessorIdentity.outputs.clientId
