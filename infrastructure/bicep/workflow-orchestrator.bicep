// workflow-orchestrator.bicep
//
// Resource-group-scoped entry point for workflow-orchestrator's own slice of
// infrastructure: the Document Orchestration Durable Functions app (Flex
// Consumption Function App + hosting plan), its Application Insights
// component, its inbound private endpoint, and the data-plane role
// assignments its system-assigned identity needs. Same shape and same
// shared modules as intake-processor.bicep -- see that file's header for
// why each service's compute is its own entry point rather than part of
// main.bicep.
//
// Deploy this into the resource group holding the pipeline's shared data
// resources (rg-rsbc-dmer-dev in DEV): the blob-container and Key Vault role
// assignments below reference those resources as `existing` in this
// template's own resource group. Service Bus may live elsewhere
// (serviceBusResourceGroupName), as in intake-processor.bicep.
//
// Not created here, referenced as already existing:
// - The VNet integration subnet (delegated to Microsoft.App/environments) --
//   shared by both orchestrators (Document and, later, Driver), so neither
//   app's template owns it; created with modules/networking/subnet.bicep
//   (see deployment/dev/README.md).
// - The Durable Functions runtime storage account (AzureWebJobsStorage +
//   deployment package container), reached by connection string like
//   intake-processor's. Durable Functions' default Azure Storage backend
//   keeps its control/work-item queues, History/Instances tables and lease
//   blobs there, so that account needs blob, queue AND table private
//   endpoints -- not just blob.
// - The dmer-extracted / driver-decision queues and the extracted-dmer /
//   normalized-dmer / rules containers -- owned by main.bicep.
// - The Postgres role for this app's identity (data-plane SQL -- run
//   services/intake-processor/create-principal.sql + roles.sql with
//   function_app_name = this app's name; see services/workflow-orchestrator/README.md).
//
// The StartDocumentOrchestration trigger is DISABLED unless
// documentOrchestrationTriggerEnabled is true -- via the
// AzureWebJobs.StartDocumentOrchestration.Disabled app setting, the same
// setting the Portal's Enable/Disable buttons flip. While disabled, messages
// wait on dmer-extracted; nothing else in this app starts work on its own.

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

@description('Function App name.')
@minLength(2)
param functionAppName string

@description('Flex Consumption hosting plan name (SKU FC1). One app per Flex plan, so the Driver Orchestration gets its own.')
@minLength(1)
param hostingPlanName string

@description('Resource ID of the existing subnet (delegated to Microsoft.App/environments) this Function App integrates into -- shared with the Driver Orchestration app.')
@minLength(1)
param virtualNetworkSubnetId string

@description('Resource ID of the existing private-endpoint subnet this Function App\'s inbound private endpoint is placed in.')
@minLength(1)
param privateEndpointSubnetId string

@description('Whether the Function App accepts public-internet traffic (including its SCM deploy endpoint). Enabled lets code be published from outside the VNet.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('Name of the existing storage account backing AzureWebJobsStorage (Durable Functions state) and the deployment package container.')
@minLength(1)
param runtimeStorageAccountName string

@description('Blob container on runtimeStorageAccountName holding the Flex Consumption deployment package. Must exist before the first publish.')
@minLength(1)
param deploymentPackageContainerName string

@secure()
@description('Connection string (account key) for runtimeStorageAccountName -- used for both AzureWebJobsStorage and DEPLOYMENT_STORAGE_CONNECTION_STRING. Supply at deploy time, never checked into a parameters.json file.')
param runtimeStorageConnectionString string

@description('Resource ID of the Log Analytics Workspace the Application Insights component reports into.')
@minLength(1)
param logAnalyticsWorkspaceId string

@description('Python runtime version for the Flex Consumption worker.')
param pythonVersion string = '3.12'

@description('Maximum scale-out instance count. Kept low in DEV: each instance takes an IP in the shared orchestrator subnet.')
@minValue(1)
param maximumInstanceCount int = 10

@description('Whether the StartDocumentOrchestration Service Bus trigger runs. False deploys the app with the trigger disabled, so messages wait on dmer-extracted.')
param documentOrchestrationTriggerEnabled bool = false

@description('Fully-qualified Service Bus namespace hostname, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net.')
@minLength(1)
param serviceBusNamespaceFqdn string

@description('Resource group containing the Service Bus namespace.')
@minLength(1)
param serviceBusResourceGroupName string

@description('Queue the StartDocumentOrchestration trigger consumes (see docs/development/message-contracts.md).')
param dmerExtractedQueueName string = 'dmer-extracted'

@description('Session-enabled queue the Signal Driver activity publishes to (see docs/development/message-contracts.md).')
param driverDecisionQueueName string = 'driver-decision'

@description('First retry interval (seconds) for every activity call.')
param retryFirstIntervalSeconds string = '30'

@description('Max attempts (including the first) for every activity call.')
param retryMaxAttempts string = '3'

@description('Postgres Flexible Server hostname. The role for this app\'s identity is created out-of-band via SQL -- see this file\'s header.')
@minLength(1)
param postgresHost string

@description('Postgres port.')
param postgresPort string = '5432'

@description('Postgres database name.')
param postgresDatabase string = 'dmer'

@description('Name of the existing pipeline data storage account (in this resource group) holding extracted-dmer, normalized-dmer and rules.')
@minLength(1)
param dataStorageAccountName string

@description('Container Normalize writes normalized DMERs to.')
param normalizedDmerContainer string = 'normalized-dmer'

@description('Container holding the rule engine\'s rule set.')
param rulesContainer string = 'rules'

@description('Blob path of the active rule set within rulesContainer.')
param rulesActivePath string = 'active/rules.json'

@description('Mercury driver-by-licence API base URL (Driver Lookup).')
param mercuryDriverLicenceApiBaseUrl string = ''

@secure()
@description('Bearer token for the Mercury API. Supply at deploy time, never checked into a parameters.json file.')
param mercuryApiKey string = ''

@description('Mercury document types that count toward a driver\'s evaluation (comma-separated).')
param mercuryCountedDocumentTypes string = 'DMER'

@description('Mercury document statuses that don\'t count (comma-separated).')
param mercuryUncountedDocumentStatuses string = 'Rejected'

@description('External Azure OpenAI (AI Hub) endpoint for Normalize.')
@minLength(1)
param azureOpenAiEndpoint string

@description('Azure OpenAI deployment name, e.g. gpt-5.1.')
@minLength(1)
param azureOpenAiDeployment string

@description('Azure OpenAI API version.')
@minLength(1)
param azureOpenAiApiVersion string

@description('Name of the existing Key Vault (in this resource group) holding the Azure OpenAI API key -- resolved as a Key Vault reference by this app\'s identity.')
@minLength(1)
param keyVaultName string

@description('Name of the Key Vault secret holding the Azure OpenAI API key.')
param azureOpenAiApiKeySecretName string = 'azure-openai-api-key'

@description('Normalize category-pass temperature.')
param normalizationCategoryTemperature string = '0.0'

@description('Normalize analyze-pass temperature.')
param normalizationAnalyzeTemperature string = '0.0'

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'workflow-orchestrator', costCenter, owner, dataClassification)
var serviceBusNamespaceName = split(serviceBusNamespaceFqdn, '.')[0]
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var storageBlobDataReaderRoleId = '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1'
var storageBlobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'
var keyVaultSecretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

// ---------------------------------------------------------------------------
// 1. Application Insights
// ---------------------------------------------------------------------------
module appInsights 'modules/monitor/application-insights.bicep' = {
  name: '${deployment().name}-appinsights'
  params: {
    name: functionAppName
    location: location
    tags: serviceTags
    workspaceResourceId: logAnalyticsWorkspaceId
  }
}

// ---------------------------------------------------------------------------
// 2. Function App (+ hosting plan)
//
// POSTGRES_USER is the Function App's own name: with a system-assigned
// identity, the Entra principal registered against Postgres is named after
// the resource itself (same as intake-processor).
// ---------------------------------------------------------------------------
var appSettings = {
  AzureWebJobsStorage: runtimeStorageConnectionString
  DEPLOYMENT_STORAGE_CONNECTION_STRING: runtimeStorageConnectionString
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.outputs.connectionString
  'AzureWebJobs.StartDocumentOrchestration.Disabled': documentOrchestrationTriggerEnabled ? 'false' : 'true'
  // Trigger binding's identity-based connection (resolved by the host) vs.
  // the FQDN the Signal Driver activity's own client uses -- same split as
  // intake-processor.bicep.
  ServiceBusConnection__fullyQualifiedNamespace: serviceBusNamespaceFqdn
  SERVICE_BUS_NAMESPACE_FQDN: serviceBusNamespaceFqdn
  DMER_EXTRACTED_QUEUE: dmerExtractedQueueName
  DRIVER_DECISION_QUEUE: driverDecisionQueueName
  ORCHESTRATION_RETRY_FIRST_INTERVAL_SECONDS: retryFirstIntervalSeconds
  ORCHESTRATION_RETRY_MAX_ATTEMPTS: retryMaxAttempts
  POSTGRES_HOST: postgresHost
  POSTGRES_PORT: postgresPort
  POSTGRES_DATABASE: postgresDatabase
  POSTGRES_USER: functionAppName
  POSTGRES_SSLMODE: 'require'
  BLOB_ACCOUNT_URL: 'https://${dataStorageAccountName}.blob.${az.environment().suffixes.storage}/'
  NORMALIZED_DMER_CONTAINER: normalizedDmerContainer
  RULES_CONTAINER: rulesContainer
  RULES_ACTIVE_PATH: rulesActivePath
  MERCURY_DRIVER_LICENCE_API_BASE_URL: mercuryDriverLicenceApiBaseUrl
  MERCURY_API_KEY: mercuryApiKey
  MERCURY_COUNTED_DOCUMENT_TYPES: mercuryCountedDocumentTypes
  MERCURY_UNCOUNTED_DOCUMENT_STATUSES: mercuryUncountedDocumentStatuses
  AZURE_OPENAI_ENDPOINT: azureOpenAiEndpoint
  // The documented Managed Identity exception (external AI Hub key) --
  // resolved from Key Vault by this app's identity, never stored here.
  AZURE_OPENAI_API_KEY: '@Microsoft.KeyVault(VaultName=${keyVaultName};SecretName=${azureOpenAiApiKeySecretName})'
  AZURE_OPENAI_DEPLOYMENT: azureOpenAiDeployment
  AZURE_OPENAI_API_VERSION: azureOpenAiApiVersion
  NORMALIZATION_CATEGORY_TEMPERATURE: normalizationCategoryTemperature
  NORMALIZATION_ANALYZE_TEMPERATURE: normalizationAnalyzeTemperature
}

module functionApp 'modules/compute/function-app.bicep' = {
  name: '${deployment().name}-function-app'
  params: {
    name: functionAppName
    location: location
    tags: serviceTags
    siteExtraTags: {
      'hidden-link: /app-insights-resource-id': appInsights.outputs.id
    }
    hostingPlanName: hostingPlanName
    virtualNetworkSubnetId: virtualNetworkSubnetId
    publicNetworkAccess: publicNetworkAccess
    storageAccountName: runtimeStorageAccountName
    deploymentPackageContainerName: deploymentPackageContainerName
    pythonVersion: pythonVersion
    maximumInstanceCount: maximumInstanceCount
    appSettings: appSettings
  }
}

// ---------------------------------------------------------------------------
// 3. Private endpoint -- inbound access to this Function App.
// ---------------------------------------------------------------------------
module privateEndpoint 'modules/networking/private-endpoint.bicep' = {
  name: '${deployment().name}-pe'
  params: {
    name: 'pe-${functionAppName}'
    location: location
    tags: serviceTags
    subnetId: privateEndpointSubnetId
    targetResourceId: functionApp.outputs.id
    groupIds: [
      'sites'
    ]
  }
}

// ---------------------------------------------------------------------------
// 4. Service Bus RBAC (queue-scoped): receive dmer-extracted (the trigger),
//    send driver-decision (Signal Driver).
// ---------------------------------------------------------------------------
module dmerExtractedReceiverRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-extracted-receive'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: dmerExtractedQueueName
    principalId: functionApp.outputs.principalId
    roleDefinitionId: serviceBusDataReceiverRoleId
  }
}

module driverDecisionSenderRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-driver-decision-send'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: driverDecisionQueueName
    principalId: functionApp.outputs.principalId
    roleDefinitionId: serviceBusDataSenderRoleId
  }
}

// ---------------------------------------------------------------------------
// 5. Blob RBAC (container-scoped, least privilege): Normalize reads the
//    extraction from extracted-dmer and writes normalized-dmer; the Rule
//    Engine reads the active rule set from rules.
// ---------------------------------------------------------------------------
resource extractedContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${dataStorageAccountName}/default/extracted-dmer'
}

resource normalizedContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${dataStorageAccountName}/default/${normalizedDmerContainer}'
}

resource rulesContainerExisting 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${dataStorageAccountName}/default/${rulesContainer}'
}

resource extractedBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(extractedContainer.id, functionAppName, storageBlobDataReaderRoleId)
  scope: extractedContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: functionApp.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource normalizedBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(normalizedContainer.id, functionAppName, storageBlobDataContributorRoleId)
  scope: normalizedContainer
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataContributorRoleId)
    principalId: functionApp.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

resource rulesBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(rulesContainerExisting.id, functionAppName, storageBlobDataReaderRoleId)
  scope: rulesContainerExisting
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', storageBlobDataReaderRoleId)
    principalId: functionApp.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// 6. Key Vault RBAC -- resolves the AZURE_OPENAI_API_KEY reference. Scoped
//    to the one secret, not the vault.
// ---------------------------------------------------------------------------
resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

resource openAiKeySecret 'Microsoft.KeyVault/vaults/secrets@2023-07-01' existing = {
  parent: keyVault
  name: azureOpenAiApiKeySecretName
}

resource openAiKeySecretsUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(openAiKeySecret.id, functionAppName, keyVaultSecretsUserRoleId)
  scope: openAiKeySecret
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleId)
    principalId: functionApp.outputs.principalId
    principalType: 'ServicePrincipal'
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output functionAppName string = functionApp.outputs.name
output functionAppPrincipalId string = functionApp.outputs.principalId
output functionAppDefaultHostName string = functionApp.outputs.defaultHostName
output applicationInsightsName string = appInsights.outputs.name
