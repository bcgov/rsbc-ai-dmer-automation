// account-role-assignment.bicep
//
// Purpose: Role assignments scoped to a whole EXISTING storage account,
// deployable into that account's own resource group. Used by
// modules/compute/function-app.bicep to give a Function App's identity the
// data-plane roles its identity-based AzureWebJobsStorage connection and
// deployment package link need on the shared runtime storage account
// (rsbcstorage in dev), which lives outside the app's resource group.

@description('Name of the existing storage account.')
param storageAccountName string

@description('Principal (object) ID receiving the roles.')
param principalId string

@description('Role definition IDs (GUIDs), e.g. b7e6dc6d-f1e8-4753-8033-0f276bb0955b for Storage Blob Data Owner.')
param roleDefinitionIds array

@description('Principal type of principalId.')
@allowed([
  'ServicePrincipal'
  'User'
  'Group'
])
param principalType string = 'ServicePrincipal'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource assignments 'Microsoft.Authorization/roleAssignments@2022-04-01' = [
  for roleDefinitionId in roleDefinitionIds: {
    name: guid(storageAccount.id, principalId, roleDefinitionId)
    scope: storageAccount
    properties: {
      principalId: principalId
      principalType: principalType
      roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
    }
  }
]
