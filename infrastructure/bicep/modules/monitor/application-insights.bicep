// application-insights.bicep
//
// Reusable Application Insights component, workspace-based (Log Analytics
// backed) rather than classic — classic Application Insights has been
// deprecated by Microsoft for new resources.
//
// This module only links to a Log Analytics Workspace via
// `workspaceResourceId`; it never creates one. That resource ID can point
// at a workspace this repo creates (see log-analytics-workspace.bicep) or
// an existing one it doesn't own — e.g. the subscription/region's
// auto-provisioned default workspace (DefaultWorkspace-<subscriptionId>-<region>,
// which is where DEV's intake-processor App Insights component already
// reports, since it predates this module).

@description('Application Insights component name.')
@minLength(1)
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the Log Analytics Workspace this component reports into.')
@minLength(1)
param workspaceResourceId string

@description('Application Insights application type.')
param applicationType string = 'web'

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: name
  location: location
  tags: tags
  kind: applicationType
  properties: {
    Application_Type: applicationType
    WorkspaceResourceId: workspaceResourceId
    IngestionMode: 'LogAnalytics'
  }
}

output id string = appInsights.id
output name string = appInsights.name
output connectionString string = appInsights.properties.ConnectionString
output instrumentationKey string = appInsights.properties.InstrumentationKey
