// container-apps-environment.bicep
//
// Purpose: Container Apps environment (workload profiles), VNet-integrated and
// internal-only, with app logs sent to a Log Analytics workspace. Shared by
// every containerized pipeline stage (di-processor first; later stages run in
// the same environment).
//
// Networking:
//   * infrastructureSubnetId must be an EXISTING subnet delegated to
//     Microsoft.App/environments (min /27; /26 recommended when shared), e.g.
//     snet-rsbc-dmer-cae-dev, created with modules/networking/subnet.bicep in
//     the platform VNet's resource group.
//   * internal: true -- no public IP or public ingress (landing-zone policy);
//     apps reach Postgres/Service Bus/DI/Blob over the VNet's private
//     endpoints.
//   * Outbound goes through the hub firewall (the landing zone's subnets have
//     no default outbound access). The environment needs egress to MCR,
//     Microsoft Entra ID and Azure Monitor (and the app's registry / APIs);
//     without those firewall rules the environment can provision but apps
//     cannot pull images or get tokens.
//   * The subnet and internal mode are fixed at creation -- changing either
//     means recreating the environment.

@description('Environment name, e.g. cae-rsbc-dmer-shared-dev-001.')
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the existing subnet delegated to Microsoft.App/environments.')
param infrastructureSubnetId string

@description('Resource ID of the Log Analytics workspace for app/system logs. Empty = no log destination (logs only via diagnostic settings, if any).')
param logAnalyticsWorkspaceId string = ''

@description('Spread the environment across availability zones. Off for dev; the subnet must be sized for it when on.')
param zoneRedundant bool = false

var logAnalyticsSubscriptionId = empty(logAnalyticsWorkspaceId) ? '' : split(logAnalyticsWorkspaceId, '/')[2]
var logAnalyticsResourceGroup = empty(logAnalyticsWorkspaceId) ? '' : split(logAnalyticsWorkspaceId, '/')[4]
var logAnalyticsName = empty(logAnalyticsWorkspaceId) ? '' : last(split(logAnalyticsWorkspaceId, '/'))

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = if (!empty(logAnalyticsWorkspaceId)) {
  name: logAnalyticsName
  scope: resourceGroup(logAnalyticsSubscriptionId, logAnalyticsResourceGroup)
}

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infrastructureSubnetId
      internal: true
    }
    workloadProfiles: [
      {
        name: 'Consumption'
        workloadProfileType: 'Consumption'
      }
    ]
    zoneRedundant: zoneRedundant
    appLogsConfiguration: empty(logAnalyticsWorkspaceId)
      ? null
      : {
          destination: 'log-analytics'
          logAnalyticsConfiguration: {
            customerId: workspace!.properties.customerId
            sharedKey: workspace!.listKeys().primarySharedKey
          }
        }
  }
}

@description('Resource ID of the environment — main.bicep\'s containerAppsEnvironmentId.')
output id string = environment.id

@description('Name of the environment.')
output name string = environment.name

@description('Default domain of the environment (internal apps resolve under it).')
output defaultDomain string = environment.properties.defaultDomain

@description('Static (internal load balancer) IP of the environment.')
output staticIp string = environment.properties.staticIp
