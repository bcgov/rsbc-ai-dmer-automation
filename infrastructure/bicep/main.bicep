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

// --- di-processor Container App inputs -------------------------------------
// The Container Apps Environment and Service Bus namespace are shared/other-
// workstream resources not provisioned by this template; they are supplied by
// ID/FQDN (the same "platform/shared resource passed in by ID" pattern used for
// privateEndpointSubnetId and the Private DNS zones above). See tasks.md §11.

@description('Resource ID of the shared Container Apps Environment di-processor runs in (provisioned by another workstream, passed in by ID). Empty = di-processor Container App not deployed by this template yet.')
param containerAppsEnvironmentId string = ''

@description('Fully-qualified di-processor container image, e.g. myacr.azurecr.io/di-processor:1.0.0. Required when containerAppsEnvironmentId is supplied.')
param diProcessorImage string = ''

@description('Service Bus namespace FQDN the di-processor KEDA scaler watches, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net (shared resource, passed in by FQDN). Required when containerAppsEnvironmentId is supplied.')
param serviceBusNamespaceFqdn string = ''

@description('Optional container registry login server for image pull via the di-processor Managed Identity (e.g. myacr.azurecr.io). Empty = public image / no registry auth.')
param containerRegistryServer string = ''

@description('Optional Log Analytics Workspace resource ID for di-processor Container App diagnostics (shared resource, passed in by ID). Empty = diagnostics not attached here.')
param logAnalyticsWorkspaceId string = ''

@description('PostgreSQL flexible server host for di-processor, e.g. psql-rsbc-dmer-shared-dev-001.postgres.database.azure.com (shared resource, other workstream). Required when containerAppsEnvironmentId is supplied.')
param postgresHost string = ''

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
    { name: 'SERVICE_BUS_NAMESPACE_FQDN', value: serviceBusNamespaceFqdn }
    { name: 'POSTGRES_HOST', value: postgresHost }
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
    serviceBusNamespaceFqdn: serviceBusNamespaceFqdn
    scaleQueueName: 'dmer-raw'
    minReplicas: environment == 'prod' ? 1 : 0
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    environmentVariables: diProcessorEnvironmentVariables
    openAiApiKeySecretUri: openAiApiKeySecretUri
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

@description('Name of the di-processor Container App, or empty when not deployed by this template (no Container Apps Environment ID supplied).')
output diProcessorContainerAppName string = diProcessorContainerApp.?outputs.name ?? ''
