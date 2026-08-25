// storage-account.bicep
//
// Purpose: Storage Account (StorageV2), private endpoint, and network ACLs.
//
// Network model (intentional, not an oversight):
//   publicNetworkAccess stays 'Enabled' with networkAcls.defaultAction 'Deny'.
//   This is required — not just permitted — because Document Intelligence's
//   managed-identity-based access to training/source blobs relies on the
//   storage firewall's "resource instance rule" + "trusted Azure services"
//   bypass (networkAcls.resourceAccessRules). That mechanism only evaluates
//   when public network access is not hard-disabled; if publicNetworkAccess
//   were set to 'Disabled', Document Intelligence would lose its documented
//   path to read blobs and custom model training/build calls would fail.
//   See docs/deployment/environment-setup.md and the tunnelling analysis in
//   docs/deployment/deployment-guide.md for the full explanation.
//
//   With defaultAction 'Deny' and no ipRules/virtualNetworkRules, the account
//   is still unreachable from the public internet for anything other than the
//   explicitly trusted resource instance(s) passed in `trustedResourceIds` —
//   all other data-plane traffic (e.g. from a developer's browser or a
//   different service) must go through the private endpoint.

@description('Storage account name — no dashes, lowercase, <=24 chars.')
@minLength(3)
@maxLength(24)
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Storage SKU — environment-specific (e.g. Standard_LRS in dev, Standard_ZRS/GRS in prod). Set via deployment/<env>/parameters.json.')
param skuName string = 'Standard_LRS'

@description('Resource IDs granted storage firewall "resource instance rule" trusted access (e.g. the Document Intelligence account). Required for DI custom model training against this account — see module header.')
param trustedResourceIds array = []

@description('Resource ID of the existing subnet (platform-provided) the private endpoint NIC is placed in.')
param privateEndpointSubnetId string

@description('Optional resource ID of an existing (platform/hub-managed) Private DNS Zone for privatelink.blob.core.windows.net. Leave empty if the platform team registers it out of band.')
param privateDnsZoneResourceIdBlob string = ''

@description('Name of the private endpoint resource for the blob sub-resource.')
param privateEndpointName string

@description('Whether Shared Key (account key) access is allowed. Managed Identity/RBAC (Azure AD) access is preferred; disable this once every consumer authenticates via Azure AD.')
param allowSharedKeyAccess bool = true

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
    allowBlobPublicAccess: false
    allowSharedKeyAccess: allowSharedKeyAccess
    defaultToOAuthAuthentication: true
    allowCrossTenantReplication: false
    publicNetworkAccess: 'Enabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass: 'AzureServices'
      virtualNetworkRules: []
      ipRules: []
      resourceAccessRules: [
        for resourceId in trustedResourceIds: {
          tenantId: subscription().tenantId
          resourceId: resourceId
        }
      ]
    }
  }
}

module privateEndpoint '../networking/private-endpoint.bicep' = {
  name: '${deployment().name}-pe-blob'
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    subnetId: privateEndpointSubnetId
    targetResourceId: storageAccount.id
    groupIds: [
      'blob'
    ]
    privateDnsZoneResourceId: privateDnsZoneResourceIdBlob
  }
}

@description('Resource ID of the storage account.')
output id string = storageAccount.id

@description('Name of the storage account.')
output name string = storageAccount.name

@description('Primary blob endpoint.')
output blobEndpoint string = storageAccount.properties.primaryEndpoints.blob
