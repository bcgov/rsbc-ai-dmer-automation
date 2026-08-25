// managed-identity.bicep
//
// Purpose: User-assigned Managed Identity per service.
//
// RBAC role assignments are deliberately NOT created inside this module:
// a role assignment needs both this identity's principal ID and the target
// resource's ID, and this module only knows about the identity. Role
// assignments are declared in main.bicep (the orchestration layer) once both
// sides exist — see main.bicep and docs/architecture/repository-design.md §11
// for the identity → RBAC → resource mapping.

@description('Name of the user-assigned managed identity, e.g. id-rsbc-dmer-di-processor-dev-001.')
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: name
  location: location
  tags: tags
}

@description('Resource ID of the managed identity.')
output id string = identity.id

@description('Principal (object) ID of the managed identity — used for RBAC role assignments.')
output principalId string = identity.properties.principalId

@description('Client ID of the managed identity — used by application code for DefaultAzureCredential.')
output clientId string = identity.properties.clientId
