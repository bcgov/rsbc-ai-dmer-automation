// keyvault.bicep
//
// Purpose: Key Vault (RBAC authorization, private endpoint, purge protection)
// for the few secrets the pipeline cannot avoid -- today only the external
// AI Hub API key (the one documented Managed Identity exception), read by the
// di-processor Container App through a Key Vault secret reference
// (modules/compute/container-app.bicep, openAiApiKeySecretUri).
//
// * RBAC authorization, not access policies: access is Azure role
//   assignments (Key Vault Secrets User for apps, Secrets Officer for people).
// * Public network access disabled; reachable only through the private
//   endpoint. Setting a secret therefore has to run from inside the VNet
//   (e.g. the jump box). Private DNS: none passed by default -- the landing
//   zone's dine-private-dns-zones policy registers the endpoint in the hub's
//   privatelink.vaultcore.azure.net zone.
// * Soft delete (always on) + purge protection: a deleted vault/secret is
//   recoverable for softDeleteRetentionInDays, and cannot be purged early.
//   Purge protection can't be turned off once enabled, and the vault name
//   stays reserved while soft-deleted.
//
// Secrets themselves are never created here -- no secret value ever passes
// through Bicep parameters or deployment history.

@description('Vault name: globally unique, 3-24 characters, e.g. kv-rsbc-dmer-dev-001.')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the existing private-endpoint subnet, e.g. snet-rsbc-dmer-pe-dev-001.')
param privateEndpointSubnetId string

@description('Name of the private endpoint, e.g. pe-kv-rsbc-dmer-dev-001.')
param privateEndpointName string

@description('Optional private DNS zone (privatelink.vaultcore.azure.net) resource ID. Empty = rely on the landing zone\'s DNS policy.')
param privateDnsZoneResourceId string = ''

@description('Days a deleted vault/secret is kept recoverable (7-90).')
@minValue(7)
@maxValue(90)
param softDeleteRetentionInDays int = 90

@description('Principal IDs granted Key Vault Secrets User (read secrets), e.g. the di-processor Managed Identity\'s principalId.')
param secretReaderPrincipalIds array = []

var secretsUserRoleId = '4633458b-17de-408a-b874-0445c86b69e6'

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: {
      family: 'A'
      name: 'standard'
    }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: softDeleteRetentionInDays
    enablePurgeProtection: true
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
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
    targetResourceId: vault.id
    groupIds: [
      'vault'
    ]
    privateDnsZoneResourceId: privateDnsZoneResourceId
  }
}

resource secretReaders 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for principalId in secretReaderPrincipalIds: {
    name: guid(vault.id, principalId, secretsUserRoleId)
    scope: vault
    properties: {
      principalId: principalId
      principalType: 'ServicePrincipal'
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretsUserRoleId)
    }
  }
]

@description('Resource ID of the vault.')
output id string = vault.id

@description('Vault URI, e.g. https://kv-rsbc-dmer-dev-001.vault.azure.net/ -- secret URIs are <uri>secrets/<name>.')
output uri string = vault.properties.vaultUri
