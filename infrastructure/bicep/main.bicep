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
// NOTE: Azure OpenAI is NOT provisioned here. It is hosted in a separate Azure
// AI Hub/AI Foundry project in a separate subscription and consumed via an
// endpoint URL + API key stored in Key Vault (see
// docs/architecture/repository-design.md §10 and §13, item 8). di-processor is
// wired to it below: the endpoint/deployment/API version are plain settings,
// and the key is a Container App Key Vault secret reference
// (openAiApiKeySecretUri). The Key Vault itself, and the di-processor
// identity's Key Vault Secrets User grant on it, belong to the Key Vault
// workstream and are not created here.
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

// --- di-processor Container App inputs -------------------------------------
// The Container Apps Environment and Service Bus namespace are shared/other-
// workstream resources not provisioned by this template; they are supplied by
// ID/FQDN (the same "platform/shared resource passed in by ID" pattern used for
// privateEndpointSubnetId and the Private DNS zones above). See tasks.md §11.

@description('Resource ID of the shared Container Apps Environment di-processor runs in (provisioned by another workstream, passed in by ID). Empty = di-processor Container App not deployed by this template yet.')
param containerAppsEnvironmentId string = ''

@description('Fully-qualified di-processor container image, e.g. myacr.azurecr.io/di-processor:1.0.0. Required when containerAppsEnvironmentId is supplied.')
param diProcessorImage string = ''

@description('Optional container registry login server for image pull via the di-processor Managed Identity (e.g. myacr.azurecr.io). Empty = public image / no registry auth.')
param containerRegistryServer string = ''

@description('Optional Log Analytics Workspace resource ID for di-processor Container App diagnostics (shared resource, passed in by ID). Empty = diagnostics not attached here.')
param logAnalyticsWorkspaceId string = ''

@description('App Configuration endpoint, e.g. https://appcs-rsbc-dmer-shared-dev-001.azconfig.io (shared resource, other workstream). Required when containerAppsEnvironmentId is supplied.')
param appConfigurationEndpoint string = ''

@description('Document Intelligence custom DMER model id di-processor analyzes with, e.g. rsbc-ocr-dmer-v9. Required when containerAppsEnvironmentId is supplied.')
param diCustomModelId string = ''

@description('Optional LLM prompt/schema version recorded on dmer_stage_run.model_version. Empty = recorded as \'unversioned\'.')
param llmPromptVersion string = ''

@description('External Azure OpenAI (AI Hub) endpoint for di-processor handwriting reconstruction. Required when containerAppsEnvironmentId is supplied.')
param openAiEndpoint string = ''

@description('Azure OpenAI deployment name, e.g. gpt-5.1. Required when containerAppsEnvironmentId is supplied.')
param openAiDeployment string = ''

@description('Azure OpenAI API version. Required when containerAppsEnvironmentId is supplied.')
param openAiApiVersion string = ''

@description('Key Vault secret URI of the external Azure OpenAI API key (the documented Managed Identity exception), e.g. https://kv-rsbc-dmer-dev-001.vault.azure.net/secrets/azure-openai-api-key. Resolved by the Container App via the di-processor identity, which needs Key Vault Secrets User on that vault (granted by the Key Vault workstream). Required when containerAppsEnvironmentId is supplied.')
param openAiApiKeySecretUri string = ''

var sharedTags = buildTags(environment, 'shared', costCenter, owner, dataClassification)
var documentIntelligenceAccountName = resourceName('di', 'shared', environment, instance)
var storageAccountNameValue = storageAccountName(environment, location, instance)
var diProcessorIdentityName = resourceName('id', 'di-processor', environment, instance)
var serviceBusNamespaceName = resourceName('sb', 'shared', environment, instance)
var postgresServerName = resourceName('psql', 'shared', environment, instance)
// Revised architecture queues (docs/development/message-contracts.md):
// dmer-ingest and dmer-raw (Ingest stage), dmer-extracted (di-processor's
// output). The original architecture's raw-dmer-queue and
// extracted-dmer-queue are retired: intake-processor publishes to dmer-raw,
// and di-processor consumes dmer-raw and publishes to dmer-extracted.
// driver-decision belongs to a later stage not built yet -- not declared
// here until it is.
var dmerIngestQueueName = 'dmer-ingest'
var dmerRawQueueName = 'dmer-raw'
var dmerExtractedQueueName = 'dmer-extracted'
var diProcessorContainerAppName = resourceName('ca', 'di-processor', environment, instance)
var deployDiProcessorContainerApp = !empty(containerAppsEnvironmentId)

// di-processor runtime settings (services/di-processor/src/di_processor/config.py
// reads configuration from environment variables only). Endpoints of resources
// this template creates come from module outputs; shared resources come from
// parameters. AZURE_CLIENT_ID selects the user-assigned identity for
// DefaultAzureCredential. Queue / container / health-port names are left to the
// code defaults (dmer-raw, dmer-extracted, extracted-dmer, 8080). The OpenAI API
// key is not here: it is a Key Vault secret reference (openAiApiKeySecretUri).
var diProcessorEnvironmentVariables = concat(
  [
    { name: 'APP_CONFIGURATION_ENDPOINT', value: appConfigurationEndpoint }
    { name: 'SERVICE_BUS_NAMESPACE_FQDN', value: serviceBusNamespace.outputs.fullyQualifiedNamespace }
    { name: 'POSTGRES_HOST', value: postgresServer.outputs.fullyQualifiedDomainName }
    { name: 'POSTGRES_DATABASE', value: postgresDatabase.outputs.name }
    // Postgres role = the identity's name (registered by
    // services/di-processor/create-principal.sql); password = Entra token.
    { name: 'POSTGRES_USER', value: diProcessorIdentityName }
    { name: 'BLOB_ACCOUNT_URL', value: storage.outputs.blobEndpoint }
    { name: 'DOC_INTELLIGENCE_ENDPOINT', value: documentIntelligence.outputs.endpoint }
    { name: 'DI_CUSTOM_MODEL_ID', value: diCustomModelId }
    { name: 'AZURE_CLIENT_ID', value: diProcessorIdentity.outputs.clientId }
    { name: 'AZURE_OPENAI_ENDPOINT', value: openAiEndpoint }
    { name: 'AZURE_OPENAI_DEPLOYMENT', value: openAiDeployment }
    { name: 'AZURE_OPENAI_API_VERSION', value: openAiApiVersion }
  ],
  empty(llmPromptVersion) ? [] : [{ name: 'LLM_PROMPT_VERSION', value: llmPromptVersion }]
)

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
//
// di-processor's own RBAC: Document Intelligence (Cognitive Services User) and
// the blob containers (container-scoped Reader on raw, Data Contributor on
// extracted-dmer). Service Bus, Key Vault, App
// Configuration, and PostgreSQL are shared/other-service resources provisioned
// and granted by their own workstreams — out of scope for this template today.
// di-processor's runtime access to them is added when those resources are
// composed here by that work. See tasks.md §11 (scope note).
// ---------------------------------------------------------------------------
var cognitiveServicesUserRoleId = 'a97b65f3-24c7-4388-baec-2e87135dc908'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'

resource documentIntelligenceExisting 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: documentIntelligenceAccountName
  dependsOn: [
    documentIntelligence
  ]
}

// Container-scoped references for least-privilege blob RBAC. di-processor reads
// the source document from `raw` and writes all extraction artifacts
// (top-level, OCR, handwritten, combined) to `extracted-dmer` — so it gets
// Reader on the former and Data Contributor on the latter, never account-wide
// access.
resource rawContainerExisting 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${storageAccountNameValue}/default/raw'
  dependsOn: [
    blobContainers
  ]
}

resource extractedContainerExisting 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${storageAccountNameValue}/default/extracted-dmer'
  dependsOn: [
    blobContainers
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

@description('Lets id-rsbc-dmer-di-processor read the source DMER from the raw container (read-only, container-scoped).')
resource diProcessorRawBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rawContainerExisting.id, diProcessorIdentityName, storageBlobDataReaderRoleId)
  scope: rawContainerExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Lets id-rsbc-dmer-di-processor write extraction artifacts to the extracted-dmer container (container-scoped).')
resource diProcessorExtractedBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(extractedContainerExisting.id, diProcessorIdentityName, storageBlobDataContributorRoleId)
  scope: extractedContainerExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// 5. di-processor Container App
//
// Instantiated only when a Container Apps Environment ID is supplied — the
// environment and Service Bus namespace are shared/other-workstream resources
// passed in by ID/FQDN, not created here. Least-privilege RBAC for the attached
// identity is declared in §4 above. Diagnostics route to the shared Log
// Analytics workspace when its ID is supplied. See tasks.md §11.2.
// ---------------------------------------------------------------------------
module diProcessorContainerApp 'modules/compute/container-app.bicep' = if (deployDiProcessorContainerApp) {
  name: '${deployment().name}-ca-di-processor'
  params: {
    name: diProcessorContainerAppName
    location: location
    tags: sharedTags
    containerAppsEnvironmentId: containerAppsEnvironmentId
    userAssignedIdentityId: diProcessorIdentity.outputs.id
    image: diProcessorImage
    registryServer: containerRegistryServer
    serviceBusNamespaceFqdn: serviceBusNamespace.outputs.fullyQualifiedNamespace
    // di-processor consumes dmer-raw (migrated from raw-dmer-queue).
    scaleQueueName: 'dmer-raw'
    minReplicas: environment == 'prod' ? 1 : 0
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    environmentVariables: diProcessorEnvironmentVariables
    openAiApiKeySecretUri: openAiApiKeySecretUri
  }
}

// ---------------------------------------------------------------------------
// 6. Service Bus
//
// One namespace, three queues so far, all from the revised architecture
// (docs/development/message-contracts.md): dmer-ingest and dmer-raw (the
// Ingest stage) and dmer-extracted (di-processor's output). The original
// architecture's raw-dmer-queue and extracted-dmer-queue are retired — see
// the note above dmerIngestQueueName's declaration. driver-decision (the
// revised architecture's remaining queue) isn't declared yet — a later
// stage, not built. No topics: PaddleOCR isn't
// a Service Bus consumer (it's a separately-deployed Container App,
// `paddleocr-gpu-app` in this same resource group, called directly over
// HTTP by di-processor rather than via pub/sub).
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

module dmerExtractedQueue 'modules/servicebus/queue.bicep' = {
  name: '${deployment().name}-sb-dmer-extracted-queue'
  params: {
    namespaceName: serviceBusNamespace.outputs.name
    name: dmerExtractedQueueName
    maxDeliveryCount: 5
    lockDuration: 'PT5M'
    // message-contracts.md specifies only MaxDeliveryCount 5 for
    // dmer-extracted — no duplicate-detection window, so it's left disabled
    // rather than assuming a value. (di-processor's message_id is
    // deterministic per document, so enabling one later would be effective.)
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
    // message-contracts.md -- left disabled, same reasoning as dmer-extracted.
  }
}

// ---------------------------------------------------------------------------
// 7. PostgreSQL — the revised architecture's schema (see
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
// 8. Service Bus RBAC role assignments
// ---------------------------------------------------------------------------
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

resource dmerRawQueueExisting 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  name: '${serviceBusNamespaceName}/${dmerRawQueueName}'
  dependsOn: [
    dmerRawQueue
  ]
}

resource dmerExtractedQueueExisting 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' existing = {
  name: '${serviceBusNamespaceName}/${dmerExtractedQueueName}'
  dependsOn: [
    dmerExtractedQueue
  ]
}

@description('Lets id-rsbc-dmer-di-processor consume documents awaiting extraction from dmer-raw (queue-scoped).')
resource diProcessorDmerRawReceiver 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dmerRawQueueExisting.id, diProcessorIdentityName, serviceBusDataReceiverRoleId)
  scope: dmerRawQueueExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', serviceBusDataReceiverRoleId)
    principalId: diProcessorIdentity.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

@description('Lets id-rsbc-dmer-di-processor publish its result to dmer-extracted (queue-scoped).')
resource diProcessorDmerExtractedSender 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(dmerExtractedQueueExisting.id, diProcessorIdentityName, serviceBusDataSenderRoleId)
  scope: dmerExtractedQueueExisting
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
output dmerExtractedQueueName string = dmerExtractedQueue.outputs.name
output dmerIngestQueueName string = dmerIngestQueue.outputs.name
output dmerRawQueueName string = dmerRawQueue.outputs.name
output postgresServerName string = postgresServer.outputs.name
output postgresFullyQualifiedDomainName string = postgresServer.outputs.fullyQualifiedDomainName
output postgresDatabaseName string = postgresDatabase.outputs.name

@description('Name of the di-processor Container App, or empty when not deployed by this template (no Container Apps Environment ID supplied).')
output diProcessorContainerAppName string = diProcessorContainerApp.?outputs.name ?? ''
