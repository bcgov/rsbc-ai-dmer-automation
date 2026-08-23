// resource-group.bicep
//
// Purpose: Creates the application resource group (e.g. rg-rsbc-dmer-dev).
//
// Subscription-scoped — only deployable via subscription.bicep (or another
// subscription-scope template), never via main.bicep (resourceGroup scope).
//
// The landing zone VNet itself (f11861-<env>-vwan-spoke) remains
// platform-team-owned and is only referenced, never created, here — see
// modules/networking/subnet.bicep for the subnet this repository DOES
// create inside that existing VNet.

targetScope = 'subscription'

@description('Resource group name, e.g. rg-rsbc-dmer-dev.')
param name string

@description('Azure region.')
param location string = 'canadacentral'

@description('Standard resource tags.')
param tags object = {}

resource rg 'Microsoft.Resources/resourceGroups@2024-11-01' = {
  name: name
  location: location
  tags: tags
}

@description('Resource ID of the resource group.')
output id string = rg.id

@description('Name of the resource group.')
output name string = rg.name
