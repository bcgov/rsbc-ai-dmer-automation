// container-registry.bicep
//
// Purpose: Private Azure Container Registry for the pipeline's container
// images (di-processor first), reachable only through a private endpoint.
//
// Configured to the BC Gov landing zone's container-registry guardrails
// (assignment enforce-gr-contreg0 -- audit-only at the time of writing, but
// written to pass them so enforcement later doesn't break the registry):
//   * Premium SKU (deny-containerregistry-sku-privatelink)
//   * public network access disabled + private endpoint
//     (deny-containerregistry-unrestricted-network-access)
//   * no admin user / local auth (deny-containerregistry-local-auth)
//   * no anonymous pull (deny-containerregistry-anonymous-auth)
//   * export disabled (deny-containerregistry-exports)
//   * ARM-audience tokens disabled (deny-containerregistry-arm-audience)
// Not done: customer-managed-key encryption (acrcmkdeny, also audit-only) --
// needs a Key Vault key and identity; add if the platform enforces it.
//
// Consequence of "private": images can only be pushed from inside the VNet
// (the jump box, or a self-hosted CI runner), not from a laptop or a
// GitHub-hosted runner. Pulls by Container Apps in the VNet go over the
// private endpoint.
//
// Private DNS: no zone is passed by default -- the landing zone's
// dine-private-dns-zones policy registers private endpoints in the hub's
// privatelink.azurecr.io zone, as it does for the other PaaS endpoints here.

@description('Registry name: globally unique, 5-50 alphanumeric characters, no hyphens, e.g. crrsbcdmershareddev001.')
@minLength(5)
@maxLength(50)
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the existing private-endpoint subnet, e.g. snet-rsbc-dmer-pe-dev-001.')
param privateEndpointSubnetId string

@description('Name of the private endpoint, e.g. pe-cr-rsbc-dmer-shared-dev-001.')
param privateEndpointName string

@description('Optional private DNS zone (privatelink.azurecr.io) resource ID. Empty = rely on the landing zone\'s DNS policy.')
param privateDnsZoneResourceId string = ''

@description('Principal IDs granted AcrPull (e.g. the di-processor Managed Identity\'s principalId).')
param pullPrincipalIds array = []

@description('Principal IDs granted AcrPush (people or a CI identity that build images).')
param pushPrincipalIds array = []

@description('Principal type for pushPrincipalIds: User, Group or ServicePrincipal.')
@allowed([
  'User'
  'Group'
  'ServicePrincipal'
])
param pushPrincipalType string = 'User'

var acrPullRoleId = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var acrPushRoleId = '8311e382-0749-4cb8-b61a-304f252e45ec'

resource registry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: 'Premium'
  }
  properties: {
    adminUserEnabled: false
    anonymousPullEnabled: false
    publicNetworkAccess: 'Disabled'
    networkRuleBypassOptions: 'AzureServices'
    zoneRedundancy: 'Disabled'
    policies: {
      exportPolicy: {
        status: 'disabled'
      }
      azureADAuthenticationAsArmPolicy: {
        status: 'disabled'
      }
    }
  }
}

module privateEndpoint '../networking/private-endpoint.bicep' = {
  name: '${deployment().name}-pe'
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    subnetId: privateEndpointSubnetId
    targetResourceId: registry.id
    groupIds: [
      'registry'
    ]
    privateDnsZoneResourceId: privateDnsZoneResourceId
  }
}

resource pullAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in pullPrincipalIds: {
    name: guid(registry.id, principalId, acrPullRoleId)
    scope: registry
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRoleId)
    }
  }
]

resource pushAssignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in pushPrincipalIds: {
    name: guid(registry.id, principalId, acrPushRoleId)
    scope: registry
    properties: {
      principalId: principalId
      principalType: pushPrincipalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPushRoleId)
    }
  }
]

@description('Resource ID of the registry.')
output id string = registry.id

@description('Login server, e.g. crrsbcdmershareddev001.azurecr.io -- main.bicep\'s containerRegistryServer.')
output loginServer string = registry.properties.loginServer
