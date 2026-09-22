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

@description('Service Bus namespace SKU. Premium — the only tier this landing zone\'s policy allows to actually be reachable (Basic/Standard have no private endpoint option and public access is denied by policy) — see modules/servicebus/namespace.bicep\'s header.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param serviceBusSkuName string = 'Premium'

@description('Optional resource ID of the platform/hub-managed Private DNS Zone for privatelink.servicebus.windows.net. Leave empty — confirmed empty for every other private endpoint in this landing zone (see docs/deployment/deployment-guide.md, "Private DNS: confirmed behavior"); the platform\'s DINE policy registers the DNS A-record automatically regardless of resource type.')
param privateDnsZoneIdServiceBus string = ''

@description('PostgreSQL administrator login username.')
param postgresAdministratorLogin string = 'rsbc_dmer_admin'

@secure()
@description('PostgreSQL administrator login password. Supply via a pipeline secret, never a parameters.json file.')
param postgresAdministratorLoginPassword string

@description('PostgreSQL compute SKU name.')
param postgresSkuName string = 'Standard_B2s'

@description('PostgreSQL compute SKU tier.')
param postgresSkuTier string = 'Burstable'

@description('PostgreSQL storage size in GB.')
param postgresStorageSizeGB int = 32

@description('Optional resource ID of the platform/hub-managed Private DNS Zone for privatelink.postgres.database.azure.com. Leave empty — same confirmed-empty reasoning as privateDnsZoneIdServiceBus above.')
param privateDnsZoneIdPostgres string = ''

var sharedTags = buildTags(environment, 'shared', costCenter, owner, dataClassification)
var documentIntelligenceAccountName = resourceName('di', 'shared', environment, instance)
var storageAccountNameValue = storageAccountName(environment, location, instance)
var diProcessorIdentityName = resourceName('id', 'di-processor', environment, instance)
var serviceBusNamespaceName = resourceName('sb', 'shared', environment, instance)
var postgresServerName = resourceName('psql', 'shared', environment, instance)
var rawDmerQueueName = 'raw-dmer-queue'
var extractedDmerQueueName = 'extracted-dmer-queue'
// Revised architecture (docs/development/message-contracts.md) -- the
// Ingest stage's two queues. Coexists with the two above rather than
// replacing them: di-processor (raw-dmer-queue/extracted-dmer-queue's other
// consumer/producer) hasn't been rebuilt for the revised architecture yet,
// so those stay live for it. dmer-extracted and driver-decision belong to
// later stages not built yet -- not declared here until they are.
var dmerIngestQueueName = 'dmer-ingest'
var dmerRawQueueName = 'dmer-raw'

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
// 5. Service Bus
//
// One namespace, four queues so far: raw-dmer-queue/extracted-dmer-queue
// (docs/contracts/queues/*.md, original architecture — still live for
// di-processor, not yet rebuilt) and dmer-ingest/dmer-raw
// (docs/development/message-contracts.md, revised architecture — the
// Ingest stage). dmer-extracted and driver-decision (the revised
// architecture's remaining two queues) aren't declared yet — later stages,
// not built. No topics: PaddleOCR isn't a Service Bus consumer (it's a
// separately-deployed Container App, `paddleocr-gpu-app` in this same
// resource group, called directly over HTTP by di-processor rather than
// via pub/sub).
// ---------------------------------------------------------------------------
module serviceBusNamespace 'modules/servicebus/namespace.bicep' = {
  name: '${deployment().name}-sb-namespace'
  params: {
    name: serviceBusNamespaceName
    location: location
    tags: sharedTags
    skuName: serviceBusSkuName
  }
}

module serviceBusPrivateEndpoint 'modules/networking/private-endpoint.bicep' = {
  name: '${deployment().name}-sb-pe'
  params: {
    name: resourceName('pe-sb', 'shared', environment, instance)
    location: location
    tags: sharedTags
    subnetId: privateEndpointSubnetId
    targetResourceId: serviceBusNamespace.outputs.id
    groupIds: [
      'namespace'
    ]
    privateDnsZoneResourceId: privateDnsZoneIdServiceBus
  }
}

module rawDmerQueue 'modules/servicebus/queue.bicep' = {
  name: '${deployment().name}-sb-raw-dmer-queue'
  params: {
    namespaceName: serviceBusNamespace.outputs.name
    name: rawDmerQueueName
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    duplicateDetectionWindow: 'PT10M'
  }
}

module extractedDmerQueue 'modules/servicebus/queue.bicep' = {
  name: '${deployment().name}-sb-extracted-dmer-queue'
  params: {
    namespaceName: serviceBusNamespace.outputs.name
    name: extractedDmerQueueName
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    // No duplicate-detection window in extracted-dmer-queue.md's contract —
    // left disabled rather than assuming a value the contract doesn't specify.
  }
}

module dmerIngestQueue 'modules/servicebus/queue.bicep' = {
  name: '${deployment().name}-sb-dmer-ingest-queue'
  params: {
    namespaceName: serviceBusNamespace.outputs.name
    name: dmerIngestQueueName
    maxDeliveryCount: 5
    // Duplicate detection window sized to the poll interval (~5 min default,
    // question I-14 may adjust) -- message-contracts.md: "window sized to
    // the poll interval", keyed on MessageId = document_guid (01-ingest.md).
    duplicateDetectionWindow: 'PT5M'
  }
}

module dmerRawQueue 'modules/servicebus/queue.bicep' = {
  name: '${deployment().name}-sb-dmer-raw-queue'
  params: {
    namespaceName: serviceBusNamespace.outputs.name
    name: dmerRawQueueName
    maxDeliveryCount: 5
    // "Lock duration 5 minutes with lock renewal during extraction"
    // (message-contracts.md) -- lock renewal is consumer-side behaviour
    // (auto-renewal on the receiver), not a queue property; nothing more
    // to configure here for it.
    lockDuration: 'PT5M'
    // No duplicate-detection window specified for dmer-raw in
    // message-contracts.md -- left disabled, same reasoning as extracted-dmer-queue.
  }
}

// ---------------------------------------------------------------------------
// 6. PostgreSQL — the revised architecture's schema (see
//    database/migrations/V0001__create_dmer_pipeline_schema.sql and
//    docs/development/data-model.md); the original architecture's
//    dmer_processing/mercury_links tables were retired in
//    database/migrations/V0002__drop_original_architecture_tables.sql once
//    the Ingest stage was rebuilt against the new schema. AAD role grants
//    for individual service identities (e.g. intake-processor's Function
//    App) are a data-plane concern, not created here — see
//    services/intake-processor/roles.sql and apply_roles.sh.
// ---------------------------------------------------------------------------
module postgresServer 'modules/database/postgresql-flexible-server.bicep' = {
  name: '${deployment().name}-psql-server'
  params: {
    name: postgresServerName
    location: location
    tags: sharedTags
    administratorLogin: postgresAdministratorLogin
    administratorLoginPassword: postgresAdministratorLoginPassword
    skuName: postgresSkuName
    skuTier: postgresSkuTier
    storageSizeGB: postgresStorageSizeGB
  }
}

module postgresDatabase 'modules/database/postgresql-database.bicep' = {
  name: '${deployment().name}-psql-database'
  params: {
    serverName: postgresServer.outputs.name
    databaseName: 'dmer'
  }
}

module postgresPrivateEndpoint 'modules/networking/private-endpoint.bicep' = {
  name: '${deployment().name}-psql-pe'
  params: {
    name: resourceName('pe-psql', 'shared', environment, instance)
    location: location
    tags: sharedTags
    subnetId: privateEndpointSubnetId
    targetResourceId: postgresServer.outputs.id
    groupIds: [
      'postgresqlServer'
    ]
    privateDnsZoneResourceId: privateDnsZoneIdPostgres
  }
}

// ---------------------------------------------------------------------------
// 7. Service Bus RBAC role assignments
// ---------------------------------------------------------------------------
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

resource rawDmerQueueExisting 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  name: '${serviceBusNamespaceName}/${rawDmerQueueName}'
  dependsOn: [
    rawDmerQueue
  ]
}

resource extractedDmerQueueExisting 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  name: '${serviceBusNamespaceName}/${extractedDmerQueueName}'
  dependsOn: [
    extractedDmerQueue
  ]
}

@description('Lets id-rsbc-dmer-di-processor consume raw-dmer-queue (intake-processor\'s output).')
resource diProcessorRawDmerReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rawDmerQueueExisting.id, diProcessorIdentityName, serviceBusDataReceiverRoleId)
  scope: rawDmerQueueExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataReceiverRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Lets id-rsbc-dmer-di-processor publish its result to extracted-dmer-queue.')
resource diProcessorExtractedDmerSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(extractedDmerQueueExisting.id, diProcessorIdentityName, serviceBusDataSenderRoleId)
  scope: extractedDmerQueueExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataSenderRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// intake-processor's own Service Bus grant lives in intake-processor.bicep
// itself now, not here -- see that template's own role-assignment module
// for why (its identity doesn't exist until that deployment creates it,
// so a grant declared here would need this template run a second time
// afterward; keeping it there avoids that entirely).

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output documentIntelligenceName string = documentIntelligence.outputs.name
output documentIntelligenceEndpoint string = documentIntelligence.outputs.endpoint
output storageAccountDeployedName string = storage.outputs.name
output storageBlobEndpoint string = storage.outputs.blobEndpoint
output diProcessorManagedIdentityClientId string = diProcessorIdentity.outputs.clientId
output serviceBusNamespaceName string = serviceBusNamespace.outputs.name
output serviceBusEndpoint string = serviceBusNamespace.outputs.serviceBusEndpoint
output rawDmerQueueName string = rawDmerQueue.outputs.name
output extractedDmerQueueName string = extractedDmerQueue.outputs.name
output dmerIngestQueueName string = dmerIngestQueue.outputs.name
output dmerRawQueueName string = dmerRawQueue.outputs.name
output postgresServerName string = postgresServer.outputs.name
output postgresFullyQualifiedDomainName string = postgresServer.outputs.fullyQualifiedDomainName
output postgresDatabaseName string = postgresDatabase.outputs.name
