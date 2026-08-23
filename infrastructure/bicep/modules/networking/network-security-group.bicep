// network-security-group.bicep
//
// Purpose: Baseline Network Security Group for the private-endpoint subnet
// this repository creates inside the existing platform VNet (see
// modules/networking/subnet.bicep). Deployed scoped to the VNet's resource
// group, not the application resource group.
//
// Only Azure's implicit default rules apply unless `securityRules` is
// supplied (deny all inbound from Internet, allow VNet-to-VNet and Azure
// Load Balancer — private endpoint traffic is unaffected by these because
// it arrives from within the VNet). No custom rules are added by default:
// confirm with the platform team whether their landing zone mandates
// specific rules (e.g. flow-log/inspection requirements) before adding any
// — see docs/deployment/environment-setup.md.

@description('NSG name, e.g. nsg-rsbc-dmer-pe-dev-001.')
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Additional security rules beyond Azure defaults. Empty by default — see module header.')
param securityRules array = []

resource nsg 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: name
  location: location
  tags: tags
  properties: {
    securityRules: securityRules
  }
}

@description('Resource ID of the NSG.')
output id string = nsg.id

@description('Name of the NSG.')
output name string = nsg.name
