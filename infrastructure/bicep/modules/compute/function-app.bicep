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
// - Both the runtime storage (AzureWebJobsStorage) and the deployment
//   package link (a blob container on `storageAccountName`, written by
//   `func azure functionapp publish` and read on every cold start) use the
//   app's Managed Identity, not an account key -- so the storage account
//   can run with shared key access disabled (MCSB IM.1). This module sets
//   `AzureWebJobsStorage__accountName` itself and grants the identity
//   Storage Blob Data Owner on the account (plus queue/table roles when
//   `durableFunctions` is set -- Durable Functions keeps its task hub in
//   queues and tables there). Callers must NOT pass AzureWebJobsStorage or
//   DEPLOYMENT_STORAGE_CONNECTION_STRING in appSettings.
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

@description('Resource group holding storageAccountName -- where this module\'s storage role assignments are deployed. Defaults to this deployment\'s own resource group.')
param storageAccountResourceGroupName string = resourceGroup().name

@description('Whether this is a Durable Functions app: also grants the identity Storage Queue and Table Data Contributor on storageAccountName, which the Durable task hub (control/work-item queues, History/Instances tables) needs.')
param durableFunctions bool = false

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

@description('Maximum scale-out instance count. Each instance takes an IP in the integration subnet, so apps sharing a small subnet should keep this low.')
@minValue(1)
@maxValue(1000)
param maximumInstanceCount int = 100

@description('App settings for this Function App (name/value pairs) — the caller assembles APPLICATIONINSIGHTS_CONNECTION_STRING plus every service-specific setting; this module adds the identity-based AzureWebJobsStorage__accountName itself. Passed as one secure object since it typically mixes secrets (connection strings, API keys) with plain config — keeping the whole thing out of deployment-history/what-if diff text is simpler and safer than splitting secret from non-secret per key.')
@secure()
param appSettings object

@description('Whether the Function App (including its SCM/Kudu deploy endpoint) accepts traffic from the public internet. Enabled lets `func azure functionapp publish` deploy from outside the VNet; Disabled restricts all inbound access to the private endpoint. Explicit here so the setting is deterministic rather than left to whatever the platform or a landing-zone policy defaults it to.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Enabled'

@description('Extra tags applied only to the Function App site, merged on top of `tags` — not the hosting plan. Use this for the "hidden-link: /app-insights-resource-id" tag Azure\'s own tooling manages: an explicit `tags` on the site resource REPLACES the whole tag set rather than merging with it, so omitting that tag here would silently delete the Portal\'s Application Insights association on the next deployment.')
param siteExtraTags object = {}

var storageBlobDataOwnerRoleId = 'b7e6dc6d-f1e8-4753-8033-0f276bb0955b'
var storageQueueDataContributorRoleId = '974c5e8b-45b9-4653-ba55-5f855dd0fb88'
var storageTableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'

var runtimeStorageSettings = {
  AzureWebJobsStorage__accountName: storageAccountName
}

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
    publicNetworkAccess: publicNetworkAccess
    virtualNetworkSubnetId: virtualNetworkSubnetId
    vnetRouteAllEnabled: false
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: 'https://${storageAccountName}.blob.${environment().suffixes.storage}/${deploymentPackageContainerName}'
          authentication: {
            type: 'SystemAssignedIdentity'
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
        for item in items(union(appSettings, runtimeStorageSettings)): {
          name: item.key
          value: string(item.value)
        }
      ]
    }
  }
}

module runtimeStorageRoles '../storage/account-role-assignment.bicep' = {
  name: '${deployment().name}-runtime-storage-roles'
  scope: resourceGroup(storageAccountResourceGroupName)
  params: {
    storageAccountName: storageAccountName
    principalId: functionApp.identity.principalId
    roleDefinitionIds: durableFunctions
      ? [
          storageBlobDataOwnerRoleId
          storageQueueDataContributorRoleId
          storageTableDataContributorRoleId
        ]
      : [
          storageBlobDataOwnerRoleId
        ]
  }
}

output id string = functionApp.id
output name string = functionApp.name
output principalId string = functionApp.identity.principalId
output defaultHostName string = functionApp.properties.defaultHostName
output hostingPlanId string = hostingPlan.id
