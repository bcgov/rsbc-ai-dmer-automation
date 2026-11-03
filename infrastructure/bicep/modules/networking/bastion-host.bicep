// bastion-host.bicep
//
// Purpose: Azure Bastion -- the only network path to rsbc-ai-jumpbox (and,
// through it, to Service Bus/Document Intelligence's private endpoints).
// Standard SKU: required for the native `az network bastion rdp`/`ssh`
// client experience this whole setup relies on (Basic SKU doesn't support
// it), and for the tunnel mode (`--disable-gateway`) used for the `scp`
// file-transfer workaround documented in docs/deployment/jumpbox-setup.md.
//
// Requires its own dedicated subnet named exactly `AzureBastionSubnet`
// (Azure's hard requirement, not a naming convention this repo chose) --
// created here, inside the existing platform VNet, the same
// cross-resource-group pattern subscription.bicep already uses for the
// private-endpoint subnet.

@description('Bastion host name, e.g. rsbc-dmer-ai-bastion.')
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Name of the resource group containing the existing platform VNet.')
@minLength(1)
param vnetResourceGroupName string

@description('Name of the existing platform-provided VNet.')
@minLength(1)
param vnetName string

@description('Address prefix for AzureBastionSubnet. Azure requires at least a /26 -- see https://learn.microsoft.com/azure/bastion/configuration-settings#subnet. Must not overlap any existing subnet in the VNet.')
@minLength(1)
param bastionSubnetAddressPrefix string

module bastionSubnet '../networking/subnet.bicep' = {
  name: '${deployment().name}-bastion-subnet'
  scope: resourceGroup(vnetResourceGroupName)
  params: {
    vnetName: vnetName
    name: 'AzureBastionSubnet'
    addressPrefix: bastionSubnetAddressPrefix
  }
}

resource publicIp 'Microsoft.Network/publicIPAddresses@2023-11-01' = {
  name: '${name}-pip'
  location: location
  tags: tags
  sku: {
    name: 'Standard'
  }
  properties: {
    publicIPAllocationMethod: 'Static'
  }
}

resource bastion 'Microsoft.Network/bastionHosts@2023-11-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    // Standard, not Basic -- see module header for why.
    name: 'Standard'
  }
  properties: {
    ipConfigurations: [
      {
        name: 'ipconfig1'
        properties: {
          subnet: {
            id: bastionSubnet.outputs.id
          }
          publicIPAddress: {
            id: publicIp.id
          }
        }
      }
    ]
  }
}

@description('Resource ID of the Bastion host.')
output id string = bastion.id

@description('Name of the Bastion host.')
output name string = bastion.name
