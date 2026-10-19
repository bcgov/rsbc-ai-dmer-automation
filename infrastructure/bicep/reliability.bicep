// reliability.bicep
//
// Resource-group-scoped entry point for the reliability components'
// infrastructure (architecture v2.1 4.6): a Flex Consumption Function App +
// its own hosting plan, its Application Insights component, and its inbound
// private endpoint. Same shape and shared modules as driver-orchestrator.bicep.
//
// The app runs timer functions only (see services/reliability/):
// - OutboxPublisher -- drains mercury_outbox to Mercury about once a minute
//   (docs/development/stages/09-reliability-components.md);
// - DlqDrain -- reads every pipeline queue's dead-letter sub-queue and
//   records, retries or routes each message (stages/10-dlq-drain.md). It
//   needs receive (the DLQ) and send (the redrive) on all four queues --
//   granted queue-scoped below;
// - ReconciliationSweeper -- every ~15 minutes, re-publishes documents
//   stalled past their stage SLA (bounded, then MANUAL_REVIEW), re-signals
//   stuck WAITING driver evaluations and logs the outbox backlog
//   (09-reliability-components.md). It sends with the same queue roles.
//
// Not created here, referenced as already existing:
// - The VNet integration subnet (delegated to Microsoft.App/environments) --
//   shared with the two orchestrator apps. Timers run as a singleton, so this
//   app needs few instances (maximumInstanceCount).
// - The runtime storage account (AzureWebJobsStorage + deployment package
//   container), reached by connection string like the orchestrators'. The
//   deployment package container must exist before the first publish.
// - The Postgres role for this app's identity (data-plane SQL -- see
//   services/reliability/README.md).
//
// Each timer is DISABLED unless its *Enabled parameter is true -- via the
// AzureWebJobs.<FunctionName>.Disabled app setting, the same setting the
// Portal's Enable/Disable buttons flip. While the publisher is disabled,
// outbox rows wait as PENDING; while the drain is, dead letters wait.

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

@description('Flex Consumption hosting plan name (SKU FC1). One app per Flex plan.')
@minLength(1)
param hostingPlanName string

@description('Resource ID of the existing subnet (delegated to Microsoft.App/environments) this Function App integrates into -- shared with the orchestrator apps.')
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

@description('Maximum scale-out instance count. Each instance takes an IP in the shared orchestrator subnet; timers run as a singleton, so a few is plenty.')
@minValue(1)
param maximumInstanceCount int = 2

@description('Whether the OutboxPublisher timer runs. False deploys the app with it disabled, so outbox rows wait as PENDING.')
param outboxPublisherEnabled bool = false

@description('NCRONTAB schedule for the OutboxPublisher (default: every minute).')
param outboxPublisherSchedule string = '0 */1 * * * *'

@description('Documents claimed per publisher run -- bounded by Mercury\'s write throttling (question M-10).')
@minValue(1)
param outboxBatchSize int = 20

@description('Attempts before an outbox row is marked FAILED (and alerted on).')
@minValue(1)
param outboxMaxAttempts int = 10

@description('Whether the DlqDrain timer runs. False deploys the app with it disabled, so dead letters wait in their DLQs.')
param dlqDrainEnabled bool = false

@description('NCRONTAB schedule for the DlqDrain (default: every 5 minutes).')
param dlqDrainSchedule string = '0 */5 * * * *'

@description('Re-sends of a retriable dead letter before the document goes to MANUAL_REVIEW.')
@minValue(0)
param dlqRedriveMaxAttempts int = 3

@description('Delay before the first re-send of a dead letter, in seconds; doubled for each further one.')
@minValue(0)
param dlqRedriveBackoffSeconds int = 300

@description('Whether the ReconciliationSweeper timer runs. False deploys the app with it disabled.')
param sweeperEnabled bool = false

@description('NCRONTAB schedule for the ReconciliationSweeper (default: every 15 minutes).')
param sweeperSchedule string = '0 */15 * * * *'

@description('Minutes a document may sit in a stage before the sweeper re-publishes it (question I-14: placeholder until answered). One value for the ingest, extraction and normalization stages and the driver evaluation.')
@minValue(1)
param stageSlaMinutes int = 30

@description('Re-publishes per document and stage before the sweeper routes it to MANUAL_REVIEW.')
@minValue(0)
param sweeperMaxRepublish int = 3

@description('Fully-qualified Service Bus namespace hostname, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net.')
@minLength(1)
param serviceBusNamespaceFqdn string

@description('Resource group containing the Service Bus namespace.')
@minLength(1)
param serviceBusResourceGroupName string

@description('Ingest queue: its dead letters are drained (and re-sent to it).')
param dmerIngestQueueName string = 'dmer-ingest'

@description('Extraction queue (DI processor): its dead letters are drained (and re-sent to it).')
param dmerRawQueueName string = 'dmer-raw'

@description('Document orchestration queue: its dead letters are drained (and re-sent to it).')
param dmerExtractedQueueName string = 'dmer-extracted'

@description('Driver decision queue (session-enabled): its dead letters are drained (and re-sent to it).')
param driverDecisionQueueName string = 'driver-decision'

@description('Mercury write-back base URL. PROVISIONAL until question M-6: the dev mock implements the assumed endpoints.')
param mercuryWriteApiBaseUrl string = ''

@description('Postgres Flexible Server hostname. The role for this app\'s identity is created out-of-band via SQL -- see this file\'s header.')
@minLength(1)
param postgresHost string

@description('Postgres port.')
param postgresPort string = '5432'

@description('Postgres database name.')
param postgresDatabase string = 'dmer'

@secure()
@description('Mercury API key, if the write endpoints need one. Supply at deploy time; never logged.')
param mercuryApiKey string = ''

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'reliability', costCenter, owner, dataClassification)

var serviceBusNamespaceName = split(serviceBusNamespaceFqdn, '.')[0]
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var pipelineQueueNames = [
  dmerIngestQueueName
  dmerRawQueueName
  dmerExtractedQueueName
  driverDecisionQueueName
]

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
// the resource itself (same as the orchestrators).
// ---------------------------------------------------------------------------
var appSettings = {
  AzureWebJobsStorage: runtimeStorageConnectionString
  DEPLOYMENT_STORAGE_CONNECTION_STRING: runtimeStorageConnectionString
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.outputs.connectionString
  'AzureWebJobs.OutboxPublisher.Disabled': outboxPublisherEnabled ? 'false' : 'true'
  OUTBOX_PUBLISHER_SCHEDULE: outboxPublisherSchedule
  OUTBOX_BATCH_SIZE: string(outboxBatchSize)
  OUTBOX_MAX_ATTEMPTS: string(outboxMaxAttempts)
  'AzureWebJobs.DlqDrain.Disabled': dlqDrainEnabled ? 'false' : 'true'
  DLQ_DRAIN_SCHEDULE: dlqDrainSchedule
  DLQ_REDRIVE_MAX_ATTEMPTS: string(dlqRedriveMaxAttempts)
  DLQ_REDRIVE_BACKOFF_SECONDS: string(dlqRedriveBackoffSeconds)
  FALLBACK_ENABLED_CATEGORIES: 'PERMANENT_BUSINESS'
  'AzureWebJobs.ReconciliationSweeper.Disabled': sweeperEnabled ? 'false' : 'true'
  SWEEPER_SCHEDULE: sweeperSchedule
  STAGE_SLA_INGEST_MINUTES: string(stageSlaMinutes)
  STAGE_SLA_EXTRACT_MINUTES: string(stageSlaMinutes)
  STAGE_SLA_NORMALIZE_MINUTES: string(stageSlaMinutes)
  STAGE_SLA_DRIVER_MINUTES: string(stageSlaMinutes)
  SWEEPER_MAX_REPUBLISH: string(sweeperMaxRepublish)
  SERVICE_BUS_NAMESPACE_FQDN: serviceBusNamespaceFqdn
  DMER_INGEST_QUEUE: dmerIngestQueueName
  DMER_RAW_QUEUE: dmerRawQueueName
  DMER_EXTRACTED_QUEUE: dmerExtractedQueueName
  DRIVER_DECISION_QUEUE: driverDecisionQueueName
  POSTGRES_HOST: postgresHost
  POSTGRES_PORT: postgresPort
  POSTGRES_DATABASE: postgresDatabase
  POSTGRES_USER: functionAppName
  POSTGRES_SSLMODE: 'require'
  MERCURY_WRITE_API_BASE_URL: mercuryWriteApiBaseUrl
  MERCURY_API_KEY: mercuryApiKey
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
// 4. Service Bus RBAC (queue-scoped): receive on each pipeline queue (the
//    DlqDrain reads its dead-letter sub-queue with the queue's own receive
//    right) and send to it (the drain's redrive, the sweeper's re-publish).
// ---------------------------------------------------------------------------
module queueReceiverRoleAssignments 'modules/servicebus/data-plane-role-assignment.bicep' = [
  for queueName in pipelineQueueNames: {
    name: '${deployment().name}-sb-role-${queueName}-receive'
    scope: resourceGroup(serviceBusResourceGroupName)
    params: {
      serviceBusNamespaceName: serviceBusNamespaceName
      queueName: queueName
      principalId: functionApp.outputs.principalId
      roleDefinitionId: serviceBusDataReceiverRoleId
    }
  }
]

module queueSenderRoleAssignments 'modules/servicebus/data-plane-role-assignment.bicep' = [
  for queueName in pipelineQueueNames: {
    name: '${deployment().name}-sb-role-${queueName}-send'
    scope: resourceGroup(serviceBusResourceGroupName)
    params: {
      serviceBusNamespaceName: serviceBusNamespaceName
      queueName: queueName
      principalId: functionApp.outputs.principalId
      roleDefinitionId: serviceBusDataSenderRoleId
    }
  }
]

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output functionAppName string = functionApp.outputs.name
output functionAppPrincipalId string = functionApp.outputs.principalId
output functionAppDefaultHostName string = functionApp.outputs.defaultHostName
output applicationInsightsName string = appInsights.outputs.name
