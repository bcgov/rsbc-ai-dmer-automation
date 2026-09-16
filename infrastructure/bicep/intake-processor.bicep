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
// main.bicep: DEV's intake-processor resources already live in
// rsbc-dmer-ai-optimization-rg, a different resource group than
// main.bicep's usual target (rg-rsbc-dmer-dev) — they were provisioned
// manually before this repo's Bicep existed. A single `main.bicep`
// deployment targets exactly one resource group, so bundling both slices
// into one unconditional template would make every deployment try to
// create the Document Intelligence/Storage slice AND the Function App
// slice in whichever resource group you happened to target. If these
// resources are ever consolidated into one resource group, folding this
// template's modules into main.bicep (behind a parameter, the same way
// every other not-yet-wired module in main.bicep's header comment is
// meant to be added) becomes straightforward — the modules themselves
// don't change either way.
//
// Not created here, referenced as already existing: the storage account
// (AzureWebJobsStorage/deployment package storage — shared with other
// things in this resource group, so not something intake-processor's own
// IaC should own), the VNet integration subnet (delegated to
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

@description('Name of the existing storage account backing AzureWebJobsStorage and the deployment package container. Referenced, not created — this account is shared with other things in the resource group.')
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

@description('Blob container that dmer_intake uploads downloaded DMER PDFs into.')
param dmerRawContainer string = 'incoming-dmer-queue'

@description('NCRONTAB schedule for the Mercury backlog poll.')
param dmerPollSchedule string = '0 */5 * * * *'

@description('Postgres Flexible Server hostname (dmer_processing / mercury_links tables). The AAD role for this Function App\'s managed identity is created out-of-band via SQL, not by this template — see docs/services/intake-processor.md.')
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

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value — DMER content is personal/medical information (FOIPPA).')
param dataClassification string = 'protected-b'

var serviceTags = buildTags(environment, 'intake-processor', costCenter, owner, dataClassification)

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
  POSTGRES_HOST: postgresHost
  POSTGRES_PORT: postgresPort
  POSTGRES_DATABASE: postgresDatabase
  POSTGRES_USER: functionAppName
  MERCURY_API_BASE_URL: mercuryApiBaseUrl
  MERCURY_API_KEY: mercuryApiKey
  MERCURY_QUEUE: mercuryQueue
  MERCURY_PAGE_SIZE: mercuryPageSize
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
    storageAccountName: storageAccountName
    deploymentPackageContainerName: deploymentPackageContainerName
    pythonVersion: pythonVersion
    appSettings: appSettings
  }
}

// ---------------------------------------------------------------------------
// Outputs
// ---------------------------------------------------------------------------
output functionAppName string = functionApp.outputs.name
output functionAppPrincipalId string = functionApp.outputs.principalId
output functionAppDefaultHostName string = functionApp.outputs.defaultHostName
output applicationInsightsName string = appInsights.outputs.name
