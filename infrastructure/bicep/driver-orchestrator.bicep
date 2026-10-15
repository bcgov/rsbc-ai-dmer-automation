// driver-orchestrator.bicep
//
// Resource-group-scoped entry point for driver-orchestrator's own slice of
// infrastructure: the Driver Orchestration Function App (Flex Consumption
// Function App + its own hosting plan -- one app per Flex plan), its
// Application Insights component, its inbound private endpoint, and the
// data-plane role assignment its system-assigned identity needs. Same shape
// and same shared modules as workflow-orchestrator.bicep.
//
// The app runs one Service Bus trigger, DriverDecision, on the session-enabled
// driver-decision queue (SessionId = driver_key), which evaluates each
// driver's batch -- see services/driver-orchestrator/README.md. It is not a
// Durable Functions app (ADR-0003; see that README), so the
// runtime storage account needs no queue/table access for Durable state.
//
// Not created here, referenced as already existing:
// - The VNet integration subnet (delegated to Microsoft.App/environments) --
//   shared with the Document Orchestration app (workflow-orchestrator.bicep).
// - The runtime storage account (AzureWebJobsStorage + deployment package
//   container), reached by connection string like workflow-orchestrator's.
//   The deployment package container must exist before the first publish.
// - The driver-decision queue -- owned by main.bicep.
// - The Postgres role for this app's identity (data-plane SQL -- see
//   services/driver-orchestrator/README.md, which reuses
//   services/intake-processor/create-principal.sql and roles.sql).
//
// The DriverDecision trigger is DISABLED unless driverDecisionTriggerEnabled
// is true -- via the AzureWebJobs.DriverDecision.Disabled app setting, the
// same setting the Portal's Enable/Disable buttons flip. While disabled,
// signals wait on driver-decision.

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

@description('Flex Consumption hosting plan name (SKU FC1). One app per Flex plan, so this app does not share the Document Orchestration\'s plan.')
@minLength(1)
param hostingPlanName string

@description('Resource ID of the existing subnet (delegated to Microsoft.App/environments) this Function App integrates into -- shared with the Document Orchestration app.')
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

@description('Name of the existing storage account backing AzureWebJobsStorage and the deployment package container.')
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

@description('Maximum scale-out instance count. Each instance takes an IP in the shared orchestrator subnet.')
@minValue(1)
param maximumInstanceCount int = 10

@description('Whether the DriverDecision Service Bus trigger runs. False deploys the app with the trigger disabled, so signals wait on driver-decision.')
param driverDecisionTriggerEnabled bool = false

@description('Fully-qualified Service Bus namespace hostname, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net.')
@minLength(1)
param serviceBusNamespaceFqdn string

@description('Resource group containing the Service Bus namespace.')
@minLength(1)
param serviceBusResourceGroupName string

@description('Session-enabled queue the DriverDecision trigger consumes (see docs/development/message-contracts.md).')
param driverDecisionQueueName string = 'driver-decision'

@description('Postgres Flexible Server hostname. The role for this app\'s identity is created out-of-band via SQL -- see this file\'s header.')
@minLength(1)
param postgresHost string

@description('Postgres port.')
param postgresPort string = '5432'

@description('Postgres database name.')
param postgresDatabase string = 'dmer'

@description('Mercury GET by driver_licence base URL (the licence is appended as a path segment) -- the Decision Gateway\'s completeness re-check.')
param mercuryDriverLicenceApiBaseUrl string = ''

@secure()
@description('Mercury API key, if the endpoint needs one. Supply at deploy time; never logged.')
param mercuryApiKey string = ''

@description('Comma-separated Mercury document types that count toward a driver\'s batch (shared with Driver Lookup).')
param mercuryCountedDocumentTypes string = 'DMER'

@description('Comma-separated Mercury document statuses that do not count (I-8: Rejected does not hold the batch up).')
param mercuryUncountedDocumentStatuses string = 'Rejected'

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'driver-orchestrator', costCenter, owner, dataClassification)
var serviceBusNamespaceName = split(serviceBusNamespaceFqdn, '.')[0]
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'

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
// the resource itself (same as workflow-orchestrator).
// ---------------------------------------------------------------------------
var appSettings = {
  AzureWebJobsStorage: runtimeStorageConnectionString
  DEPLOYMENT_STORAGE_CONNECTION_STRING: runtimeStorageConnectionString
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.outputs.connectionString
  'AzureWebJobs.DriverDecision.Disabled': driverDecisionTriggerEnabled ? 'false' : 'true'
  // Trigger binding's identity-based connection (resolved by the host) vs.
  // the FQDN the app's own client uses to schedule re-checks.
  ServiceBusConnection__fullyQualifiedNamespace: serviceBusNamespaceFqdn
  SERVICE_BUS_NAMESPACE_FQDN: serviceBusNamespaceFqdn
  DRIVER_DECISION_QUEUE: driverDecisionQueueName
  POSTGRES_HOST: postgresHost
  POSTGRES_PORT: postgresPort
  POSTGRES_DATABASE: postgresDatabase
  POSTGRES_USER: functionAppName
  POSTGRES_SSLMODE: 'require'
  MERCURY_DRIVER_LICENCE_API_BASE_URL: mercuryDriverLicenceApiBaseUrl
  MERCURY_API_KEY: mercuryApiKey
  MERCURY_COUNTED_DOCUMENT_TYPES: mercuryCountedDocumentTypes
  MERCURY_UNCOUNTED_DOCUMENT_STATUSES: mercuryUncountedDocumentStatuses
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
// 4. Service Bus RBAC (queue-scoped): receive driver-decision (the trigger)
//    and send to it (the self-scheduled re-check of a driver Mercury could
//    not verify -- see services/driver-orchestrator/src/driver_orchestrator/recheck.py).
// ---------------------------------------------------------------------------
module driverDecisionReceiverRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-driver-decision-receive'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: driverDecisionQueueName
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
// Outputs
// ---------------------------------------------------------------------------
output functionAppName string = functionApp.outputs.name
output functionAppPrincipalId string = functionApp.outputs.principalId
output functionAppDefaultHostName string = functionApp.outputs.defaultHostName
output applicationInsightsName string = appInsights.outputs.name
