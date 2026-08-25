// document-intelligence.bicep
//
// Purpose: Document Intelligence (Cognitive Services, kind FormRecognizer)
// account, private endpoint, public network access disabled.
//
// Out of scope for this module (see docs/architecture/repository-design.md
// §13 and docs/deployment/deployment-guide.md): the Document Intelligence
// Studio *project*, labeling configuration, and custom model
// training/publishing. Those are data-plane operations against this
// account's endpoint (Studio itself, the REST API, or an SDK) and are not
// ARM resources — Bicep creates the account only. See
// docs/deployment/deployment-guide.md for the recommended automation path
// (scripts/ci/document_intelligence_model.py) and its network requirements.

@description('Document Intelligence account name, e.g. di-rsbc-dmer-shared-dev-001. Also used as the required custom subdomain name.')
@minLength(2)
@maxLength(64)
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Pricing tier. S0 is the only tier that supports custom (trained) models.')
param skuName string = 'S0'

@description('Resource ID of the user-assigned managed identity used for trusted access to the training storage account.')
param userAssignedIdentityId string

@description('Resource ID of the existing subnet (platform-provided) the private endpoint NIC is placed in.')
param privateEndpointSubnetId string

@description('Optional resource ID of an existing (platform/hub-managed) Private DNS Zone for privatelink.cognitiveservices.azure.com. Leave empty if the platform team registers it out of band.')
param privateDnsZoneResourceIdCognitiveServices string = ''

@description('Name of the private endpoint resource for this account.')
param privateEndpointName string

@description('When true, disables API-key (subscription key) authentication in favour of Azure AD/RBAC only. Kept false by default to match the currently-working DEV configuration; flip once every consumer (Studio session, scripts, services) authenticates via Azure AD.')
param disableLocalAuth bool = false

resource documentIntelligence 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: name
  location: location
  tags: tags
  kind: 'FormRecognizer'
  sku: {
    name: skuName
  }
  identity: {
    type: 'SystemAssigned, UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    customSubDomainName: name
    publicNetworkAccess: 'Disabled'
    disableLocalAuth: disableLocalAuth
    networkAcls: {
      defaultAction: 'Deny'
      virtualNetworkRules: []
      ipRules: []
    }
  }
}

module privateEndpoint '../networking/private-endpoint.bicep' = {
  name: '${deployment().name}-pe-account'
  params: {
    name: privateEndpointName
    location: location
    tags: tags
    subnetId: privateEndpointSubnetId
    targetResourceId: documentIntelligence.id
    groupIds: [
      'account'
    ]
    privateDnsZoneResourceId: privateDnsZoneResourceIdCognitiveServices
  }
}

@description('Resource ID of the Document Intelligence account.')
output id string = documentIntelligence.id

@description('Name of the Document Intelligence account.')
output name string = documentIntelligence.name

@description('Data-plane endpoint, e.g. https://<name>.cognitiveservices.azure.com/. Only reachable via the private endpoint once publicNetworkAccess is Disabled.')
output endpoint string = documentIntelligence.properties.endpoint

@description('System-assigned identity principal ID.')
output systemAssignedPrincipalId string = documentIntelligence.identity.principalId
