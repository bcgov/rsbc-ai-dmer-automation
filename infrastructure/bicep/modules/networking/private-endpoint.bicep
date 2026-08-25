// private-endpoint.bicep
//
// Purpose: Reusable private endpoint + (optional) private DNS zone group for
// any PaaS resource. Used by every PaaS module (storage, Document
// Intelligence, and — later — Service Bus, PostgreSQL, Key Vault, App
// Configuration).
//
// This module only creates the Microsoft.Network/privateEndpoints resource
// and, if a private DNS zone resource ID is supplied, a DNS zone group that
// registers the endpoint's IP in that zone. It does NOT create the private
// DNS zone itself, and it does NOT create/modify the subnet — both are
// platform-team-owned resources in the existing landing zone VNet
// (see docs/deployment/environment-setup.md and
// docs/architecture/repository-design.md §11).

@description('Name of the private endpoint resource, e.g. pe-di-rsbc-dmer-shared-dev-001.')
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the existing subnet (platform-provided) the private endpoint NIC is placed in.')
param subnetId string

@description('Resource ID of the target PaaS resource this private endpoint connects to.')
param targetResourceId string

@description('Private Link group ID(s) (a.k.a. sub-resource) for the target resource, e.g. ["blob"] or ["account"].')
param groupIds array

@description('Optional: resource ID of an existing (typically platform/hub-managed) Private DNS Zone to register this endpoint in. Leave empty to skip DNS zone group creation — see the networking prerequisites note in docs/deployment/environment-setup.md.')
param privateDnsZoneResourceId string = ''

var privateDnsZoneName = empty(privateDnsZoneResourceId) ? '' : last(split(privateDnsZoneResourceId, '/'))

resource privateEndpoint 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    subnet: {
      id: subnetId
    }
    privateLinkServiceConnections: [
      {
        name: name
        properties: {
          privateLinkServiceId: targetResourceId
          groupIds: groupIds
        }
      }
    ]
  }
}

resource privateDnsZoneGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = if (!empty(privateDnsZoneResourceId)) {
  parent: privateEndpoint
  name: 'default'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: privateDnsZoneName
        properties: {
          privateDnsZoneId: privateDnsZoneResourceId
        }
      }
    ]
  }
}

@description('Resource ID of the private endpoint.')
output id string = privateEndpoint.id

@description('Name of the private endpoint.')
output name string = privateEndpoint.name

@description('Whether a DNS zone group was created (false means the platform team must register this endpoint in their private DNS zone(s) out of band).')
output dnsZoneGroupCreated bool = !empty(privateDnsZoneResourceId)
