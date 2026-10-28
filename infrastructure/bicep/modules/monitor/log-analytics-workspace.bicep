// log-analytics-workspace.bicep
//
// Reusable Log Analytics Workspace.
//
// Not currently invoked by main.bicep for intake-processor: DEV's
// Application Insights component reports into the subscription/region's
// auto-provisioned default workspace (DefaultWorkspace-<subscriptionId>-CCAN
// in DefaultResourceGroup-CCAN), which already exists and is out of this
// repo's ownership, the same way the platform VNet is. Use this module once
// a dedicated, repo-owned workspace is actually wanted — e.g. for retention
// or cost control independent of the subscription default — by passing its
// output `id` as `workspaceResourceId` to modules/monitor/application-insights.bicep.

@description('Log Analytics Workspace name.')
@minLength(4)
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Data retention, in days.')
@minValue(30)
@maxValue(730)
param retentionInDays int = 30

@description('Pricing tier / SKU.')
param skuName string = 'PerGB2018'

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    sku: {
      name: skuName
    }
    retentionInDays: retentionInDays
  }
}

output id string = workspace.id
output name string = workspace.name
