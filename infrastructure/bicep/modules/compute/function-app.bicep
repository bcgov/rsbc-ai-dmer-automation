// function-app.bicep
//
// Reusable Flex Consumption Function App module: the hosting plan (SKU
// FC1/FlexConsumption), the Function App site (Linux, Python), regional
// VNet integration into an existing delegated subnet, a system-assigned
// Managed Identity, and the app settings the caller assembles.
//
// Modeled on how DEV's intake-processor Function App was originally
// provisioned (manually, before this repo's Bicep existed) — deploy this
// with `what-if` first against an already-existing Function App to confirm
// it matches (no unexpected diffs) before trusting it to manage that
// resource going forward. See docs/services/intake-processor.md and
// docs/deployment/deployment-guide.md.
//
// Flex Consumption specifics this module assumes:
// - `virtualNetworkSubnetId` must point at a subnet already delegated to
//   `Microsoft.App/environments` (regional VNet integration) — Flex
//   Consumption doesn't support the classic Web App VNet integration
//   subnet delegation, and isn't the same subnet purpose as
//   modules/networking/subnet.bicep (private-endpoint subnets).
// - The deployment package lives in a blob container on
//   `storageAccountName`, referenced via a connection-string-named app
//   setting rather than Managed Identity — matching how this Function App
//   is deployed today (`func azure functionapp publish`, remote build).
//   Switching the deployment storage link to Managed Identity is a
//   possible follow-up, not done here in order to match live state.
// - System-assigned identity (not user-assigned) — matches what's live;
//   the Postgres AAD role for this identity's principal name is created
//   out-of-band via SQL (data-plane, not an ARM concept — see
//   docs/services/intake-processor.md), same as before this module existed.

@description('Function App name.')
@minLength(2)
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Name of the Flex Consumption hosting plan (Microsoft.Web/serverfarms, SKU FC1). Not derived from the naming convention — pin this to match whatever already exists if adopting a manually-created plan, e.g. an Azure-generated "ASP-<resourcegroup>-<hash>" name.')
@minLength(1)
param hostingPlanName string

@description('Resource ID of the existing subnet (delegated to Microsoft.App/environments) this Function App integrates into. Not created by this module.')
@minLength(1)
param virtualNetworkSubnetId string

@description('Name of the storage account backing both AzureWebJobsStorage and the Flex Consumption deployment package container. Referenced, not created, by this module — bring your own existing storage account.')
@minLength(1)
param storageAccountName string

@description('Blob container holding the Flex Consumption deployment package (created automatically by the tooling on first `func azure functionapp publish`, not by this module).')
@minLength(1)
param deploymentPackageContainerName string

@description('Python runtime version for the Flex Consumption worker.')
param pythonVersion string = '3.12'

@description('Per-instance memory (MB) for the Flex Consumption plan.')
@allowed([
  512
  2048
  4096
])
param instanceMemoryMB int = 512

@description('Maximum scale-out instance count.')
@minValue(40)
@maxValue(1000)
param maximumInstanceCount int = 100

@description('Complete app settings for this Function App (name/value pairs) — the caller assembles the full set, including AzureWebJobsStorage, DEPLOYMENT_STORAGE_CONNECTION_STRING, and APPLICATIONINSIGHTS_CONNECTION_STRING, plus every service-specific setting. Passed as one secure object since it typically mixes secrets (connection strings, API keys) with plain config — keeping the whole thing out of deployment-history/what-if diff text is simpler and safer than splitting secret from non-secret per key.')
@secure()
param appSettings object

@description('Extra tags applied only to the Function App site, merged on top of `tags` — not the hosting plan. Use this for the "hidden-link: /app-insights-resource-id" tag Azure\'s own tooling manages: an explicit `tags` on the site resource REPLACES the whole tag set rather than merging with it, so omitting that tag here would silently delete the Portal\'s Application Insights association on the next deployment.')
param siteExtraTags object = {}

resource hostingPlan 'Microsoft.Web/serverfarms@2023-12-01' = {
  name: hostingPlanName
  location: location
  tags: tags
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2023-12-01' = {
  name: name
  location: location
  tags: union(tags, siteExtraTags)
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: hostingPlan.id
    httpsOnly: true
    virtualNetworkSubnetId: virtualNetworkSubnetId
    vnetRouteAllEnabled: false
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/${deploymentPackageContainerName}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'DEPLOYMENT_STORAGE_CONNECTION_STRING'
          }
        }
      }
      runtime: {
        name: 'python'
        version: pythonVersion
      }
      scaleAndConcurrency: {
        instanceMemoryMB: instanceMemoryMB
        maximumInstanceCount: maximumInstanceCount
      }
    }
    siteConfig: {
      minTlsVersion: '1.2'
      ftpsState: 'FtpsOnly'
      use32BitWorkerProcess: false
      appSettings: [
        for item in items(appSettings): {
          name: item.key
          value: string(item.value)
        }
      ]
    }
  }
}

output id string = functionApp.id
output name string = functionApp.name
output principalId string = functionApp.identity.principalId
output defaultHostName string = functionApp.properties.defaultHostName
output hostingPlanId string = hostingPlan.id
