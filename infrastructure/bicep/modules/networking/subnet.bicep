// subnet.bicep
//
// Purpose: Creates the private-endpoint subnet inside the EXISTING
// platform-provided VNet (e.g. f11861-dev-vwan-spoke). Deployed scoped to
// the VNet's own resource group (which belongs to the platform team, not
// this repository's application resource group) — see subscription.bicep.
//
// This module does NOT create the VNet — only a subnet inside it. The VNet
// itself, its peering to the hub, flow logs, and Network Watcher remain
// platform-team-owned. See docs/deployment/environment-setup.md for how to
// confirm you have rights to create a subnet here
// (Microsoft.Network/virtualNetworks/subnets/join/action and write access
// at minimum) before running this.

@description('Name of the existing platform-provided VNet, e.g. f11861-dev-vwan-spoke.')
param vnetName string

@description('Name of the subnet to create, e.g. snet-rsbc-dmer-pe-dev-001.')
param name string

@description('Address prefix for the subnet, e.g. 10.x.x.x/27. Must not overlap any existing subnet in the VNet. Run `az network vnet subnet list` against the VNet first to pick a free range — see docs/deployment/environment-setup.md.')
param addressPrefix string

@description('Resource ID of an NSG to associate with this subnet. Leave empty to create the subnet without one (confirm this is acceptable under the platform team\'s policy first).')
param networkSecurityGroupId string = ''

resource vnet 'Microsoft.Network/virtualNetworks@2023-11-01' existing = {
  name: vnetName
}

resource subnet 'Microsoft.Network/virtualNetworks/subnets@2023-11-01' = {
  parent: vnet
  name: name
  properties: union(
    {
      addressPrefix: addressPrefix
      privateEndpointNetworkPolicies: 'Disabled'
    },
    empty(networkSecurityGroupId)
      ? {}
      : {
          networkSecurityGroup: {
            id: networkSecurityGroupId
          }
        }
  )
}

@description('Resource ID of the subnet — pass this into main.bicep\'s privateEndpointSubnetId parameter.')
output id string = subnet.id

@description('Name of the subnet.')
output name string = subnet.name
