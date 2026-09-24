// intake-processor.bicep
//
// Resource-group-scoped entry point for intake-processor's own slice of
// infrastructure: its Flex Consumption Function App (+ hosting plan) and
// Application Insights component. Composes the same shared modules listed
// in docs/architecture/repository-design.md §10
// (modules/compute/function-app.bicep, modules/monitor/application-insights.bicep)
// that every other service's compute eventually will.
//
// Why this is a separate entry point rather than another section of
// main.bicep: a single `main.bicep` deployment targets exactly one
// resource group, so bundling both slices into one unconditional template
// would make every deployment try to create the Document
// Intelligence/Storage slice AND the Function App slice in whichever
// resource group you happened to target. Keeping this as its own
// resourceGroup-scoped template (below) means it deploys into whichever
// resource group you point `--resource-group` at when running it —
// nothing in this file hardcodes a target resource group or environment
// name, so the same template works for dev/test/prod, and for a Function
// App living in the same resource group as everything else in main.bicep
// (today's DEV arrangement, both in rg-rsbc-dmer-dev) or a different one
// entirely (DEV's arrangement up until this repo's Bicep captured it,
// before intake-processor's resources were consolidated out of the
// originally-manually-created rsbc-dmer-ai-optimization-rg). If these two
// templates' resources always end up in the same resource group across
// every environment going forward, folding this template's modules into
// main.bicep (behind a parameter, the same way every other not-yet-wired
// module in main.bicep's header comment is meant to be added) becomes
// straightforward — the modules themselves don't change either way.
//
// Not created here, referenced as already existing: the storage account
// (AzureWebJobsStorage/deployment package storage — shared with other
// things in whichever resource group it lives in, so not something
// intake-processor's own IaC should own; not necessarily the same
// resource group this template deploys into, see storageAccountName's own
// description below), the VNet integration subnet (delegated to
// Microsoft.App/environments, created manually), the Postgres Flexible
// Server and its AAD role/grants for this Function App's managed identity
// (data-plane SQL, not an ARM concept — see
// docs/services/intake-processor.md), and the Log Analytics Workspace
// (this reports into the subscription/region's auto-provisioned default
// workspace — see modules/monitor/log-analytics-workspace.bicep's header).
//
// Validate with `what-if` against the live resource group before trusting
// this template to manage what's already there — see
// docs/deployment/deployment-guide.md for the general methodology
// (`az bicep build/lint` need no Azure login; `what-if`/`create` are
// control-plane-only, no VNet route needed for any step here either).

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

@description('Function App name. Not derived from the naming convention (docs/standards/naming-conventions.md) — DEV\'s was created manually without the usual "-<instance>" suffix; pin this to match whatever already exists.')
@minLength(2)
param functionAppName string

@description('Flex Consumption hosting plan name (Microsoft.Web/serverfarms, SKU FC1). Also not convention-derived — DEV\'s is an Azure-generated "ASP-<resourcegroup>-<hash>" name from when it was created via the Portal.')
@minLength(1)
param hostingPlanName string

@description('Resource ID of the existing subnet (delegated to Microsoft.App/environments) this Function App integrates into.')
@minLength(1)
param virtualNetworkSubnetId string

@description('Resource ID of the existing subnet this Function App\'s inbound private endpoint\'s NIC is placed in — the same private-endpoint subnet the jump box and (in rg-rsbc-dmer-dev) Document Intelligence/Storage/Service Bus all use. A different subnet from virtualNetworkSubnetId above: that one is for outbound VNet integration, this one is for inbound private-endpoint access to this Function App itself.')
@minLength(1)
param privateEndpointSubnetId string

@description('Whether the Function App accepts public-internet traffic (including its SCM deploy endpoint). Enabled lets code be published from outside the VNet; set Disabled per environment to restrict access to the private endpoint only.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('Name of the existing storage account backing AzureWebJobsStorage and the deployment package container. Referenced, not created — this account is shared with other things, not necessarily in the same resource group this template deploys into (the Function App module only ever builds a blob URL string from this name, never an `existing` resource lookup, so it works regardless of which resource group actually holds it).')
@minLength(1)
param storageAccountName string

@description('Blob container holding the Flex Consumption deployment package (created automatically by the tooling on first `func azure functionapp publish`).')
@minLength(1)
param deploymentPackageContainerName string

@description('Resource ID of the Log Analytics Workspace the Application Insights component reports into — see modules/monitor/log-analytics-workspace.bicep\'s header for why DEV points at the subscription/region default workspace rather than a dedicated one.')
@minLength(1)
param logAnalyticsWorkspaceId string

@description('Python runtime version for the Flex Consumption worker.')
param pythonVersion string = '3.12'

@secure()
@description('Connection string (account key) for storageAccountName — used for both AzureWebJobsStorage and DEPLOYMENT_STORAGE_CONNECTION_STRING. Supply via a pipeline secret, never checked into a parameters.json file.')
param storageAccountConnectionString string

@description('Blob container the Ingest Function writes downloaded DMER PDFs into, at raw-dmer/{yyyy}/{MM}/{document_guid}.pdf (see docs/development/stages/01-ingest.md). Renamed from the original architecture\'s "incoming-dmer-queue" to match the revised architecture\'s container name.')
param dmerRawContainer string = 'raw-dmer'

@description('NCRONTAB schedule for the Page Poller\'s Mercury backlog poll.')
param dmerPollSchedule string = '0 */5 * * * *'

@description('Service Bus queue the Page Poller/Webhook Listener publish to and the Ingest Function consumes (see docs/development/message-contracts.md).')
param dmerIngestQueueName string = 'dmer-ingest'

@description('Service Bus queue the Ingest Function publishes to once a document is downloaded (see docs/development/message-contracts.md).')
param dmerRawQueueName string = 'dmer-raw'

@description('Postgres Flexible Server hostname (dmer_document/driver/poll_checkpoint/dmer_stage_run tables -- see docs/development/data-model.md). The AAD role for this Function App\'s managed identity is created out-of-band via SQL, not by this template — see services/intake-processor/create-principal.sql + roles.sql.')
@minLength(1)
param postgresHost string

@description('Postgres port.')
param postgresPort string = '5432'

@description('Postgres database name.')
param postgresDatabase string = 'dmer'

@description('Mercury backlog API base URL (unpaginated), e.g. https://<mercury-host>/documents. Passed straight through as MERCURY_API_BASE_URL — see function_app.py\'s _get_base_mercury_url.')
param mercuryApiBaseUrl string = ''

@secure()
@description('Bearer token for the Mercury API. Supply via a pipeline secret, never checked into a parameters.json file.')
param mercuryApiKey string = ''

@description('Mercury queue selector, passed straight through as the `queue` query parameter — DPS_GENERAL | DPS_UNKNOWN | BOTH (a single combined result stream, not something this code fans out over — see docs/services/intake-processor.md).')
param mercuryQueue string = 'BOTH'

@description('Mercury API page size.')
param mercuryPageSize string = '50'

@description('Fully-qualified Service Bus namespace hostname (e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net) the Page Poller, Webhook Listener, and Ingest Function authenticate against via DefaultAzureCredential. This template grants its own managed identity queue-scoped Service Bus roles on dmer-ingest and dmer-raw -- see the role assignment modules below.')
@minLength(1)
param serviceBusNamespaceFqdn string

@description('Name of the resource group containing the Service Bus namespace, e.g. rg-rsbc-dmer-dev — may differ from this template\'s own target resource group.')
@minLength(1)
param serviceBusResourceGroupName string

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'intake-processor', costCenter, owner, dataClassification)
// Bare namespace name, derived from the FQDN parameter rather than a
// second redundant parameter -- FQDNs are always "<namespace>.servicebus.windows.net".
var serviceBusNamespaceName = split(serviceBusNamespaceFqdn, '.')[0]
var serviceBusDataSenderRoleId = '69a216fc-b8fb-44d8-bc22-1f3c2cd27a39'
var serviceBusDataReceiverRoleId = '4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0'

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
// identity, the AAD principal registered against Postgres is named after
// the resource itself (see docs/services/intake-processor.md).
// ---------------------------------------------------------------------------
var appSettings = {
  AzureWebJobsStorage: storageAccountConnectionString
  DEPLOYMENT_STORAGE_CONNECTION_STRING: storageAccountConnectionString
  APPLICATIONINSIGHTS_CONNECTION_STRING: appInsights.outputs.connectionString
  DMER_RAW_CONTAINER: dmerRawContainer
  DMER_POLL_SCHEDULE: dmerPollSchedule
  DMER_INGEST_QUEUE: dmerIngestQueueName
  DMER_RAW_QUEUE: dmerRawQueueName
  POSTGRES_HOST: postgresHost
  POSTGRES_PORT: postgresPort
  POSTGRES_DATABASE: postgresDatabase
  POSTGRES_USER: functionAppName
  MERCURY_API_BASE_URL: mercuryApiBaseUrl
  MERCURY_API_KEY: mercuryApiKey
  MERCURY_QUEUE: mercuryQueue
  MERCURY_PAGE_SIZE: mercuryPageSize
  SERVICE_BUS_NAMESPACE_FQDN: serviceBusNamespaceFqdn
  // Identity-based connection for the dmer_ingest function's Service Bus
  // *trigger* binding specifically -- a different mechanism from
  // SERVICE_BUS_NAMESPACE_FQDN above (which DefaultAzureCredential-based
  // manual sends/receives in code use). The Functions host resolves the
  // trigger's own connection via this `<prefix>__fullyQualifiedNamespace`
  // app setting convention before any of this app's Python code runs, so
  // both settings point at the same namespace but serve genuinely
  // different resolution paths.
  ServiceBusConnection__fullyQualifiedNamespace: serviceBusNamespaceFqdn
}

module functionApp 'modules/compute/function-app.bicep' = {
  name: '${deployment().name}-function-app'
  params: {
    name: functionAppName
    location: location
    tags: serviceTags
    // Preserves the Portal's Application Insights association -- see
    // function-app.bicep's siteExtraTags description.
    siteExtraTags: {
      'hidden-link: /app-insights-resource-id': appInsights.outputs.id
    }
    hostingPlanName: hostingPlanName
    virtualNetworkSubnetId: virtualNetworkSubnetId
    publicNetworkAccess: publicNetworkAccess
    storageAccountName: storageAccountName
    deploymentPackageContainerName: deploymentPackageContainerName
    pythonVersion: pythonVersion
    appSettings: appSettings
  }
}

// ---------------------------------------------------------------------------
// 3. Private endpoint -- inbound access to this Function App. Was live only
//    because someone created it by hand (in f11861-dev-networking, not this
//    resource group) before this template covered it -- deployed here into
//    the same resource group as the Function App itself, matching
//    main.bicep's convention for DI/Storage/Service Bus.
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
// 4. Service Bus RBAC -- grants this Function App's own managed identity
//    exactly the access each of its three components needs (see
//    docs/development/stages/01-ingest.md), scoped to individual queues, not
//    the whole namespace: the Page Poller/Webhook Listener send to
//    dmer-ingest, the Ingest Function receives from dmer-ingest and sends
//    to dmer-raw. Self-contained here rather than living in main.bicep:
//    this identity doesn't exist until the module above creates it, so
//    keeping the grant in the same deployment that creates the identity
//    avoids needing a second main.bicep run once this Function App exists
//    (main.bicep has no way to know this principalId otherwise, since it's
//    created in a different resource group).
// ---------------------------------------------------------------------------
module dmerIngestSenderRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-ingest-send'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: dmerIngestQueueName
    principalId: functionApp.outputs.principalId
    roleDefinitionId: serviceBusDataSenderRoleId
  }
}

module dmerIngestReceiverRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-ingest-receive'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: dmerIngestQueueName
    principalId: functionApp.outputs.principalId
    roleDefinitionId: serviceBusDataReceiverRoleId
  }
}

module dmerRawSenderRoleAssignment 'modules/servicebus/data-plane-role-assignment.bicep' = {
  name: '${deployment().name}-sb-role-raw-send'
  scope: resourceGroup(serviceBusResourceGroupName)
  params: {
    serviceBusNamespaceName: serviceBusNamespaceName
    queueName: dmerRawQueueName
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
