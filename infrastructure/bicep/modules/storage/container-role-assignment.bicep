// container-role-assignment.bicep
//
// Purpose: One role assignment scoped to a single blob container of an
// EXISTING storage account, deployable into that account's own resource
// group. Used by main.bicep to give di-processor read access to the storage
// account Ingest writes source PDFs to (rsbcstorage / raw-dmer in dev), which
// lives outside this template's resource group.

@description('Name of the existing storage account.')
param storageAccountName string

@description('Name of the existing blob container to scope the role to.')
param containerName string

@description('Principal (object) ID receiving the role.')
param principalId string

@description('Role definition ID (GUID), e.g. 2a2b9908-6ea1-4ae2-8e65-a410df84e7d1 for Storage Blob Data Reader.')
param roleDefinitionId string

@description('Principal type of principalId.')
@allowed([
  'ServicePrincipal'
  'User'
  'Group'
])
param principalType string = 'ServicePrincipal'

resource container 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' existing = {
  name: '${storageAccountName}/default/${containerName}'
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(container.id, principalId, roleDefinitionId)
  scope: container
  properties: {
    principalId: principalId
    principalType: principalType
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
  }
}
