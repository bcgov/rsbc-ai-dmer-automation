// blob-containers.bicep
//
// Purpose: Blob containers on an existing storage account. The full pipeline
// design calls for seven containers — raw, ocr, normalized, rules, audit,
// failed, archive (docs/architecture/repository-design.md §11) — but only the
// ones a currently-deployed consumer needs are created; the rest are added
// alongside the service that consumes them, via
// deployment/<env>/parameters.json (never hardcoded here).
//
// Today, only `raw` is created — it holds the Document Intelligence
// custom-model training/test document set.

@description('Name of the existing storage account to create containers on.')
param storageAccountName string

@description('Container names to create — lowercase, singular noun matching pipeline stage. See docs/standards/naming-conventions.md.')
param containerNames array = [
  'raw'
]

@description('Public access level for the containers. Must stay "None" — the storage account has allowBlobPublicAccess disabled.')
@allowed([
  'None'
])
param publicAccess string = 'None'

resource storageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

resource blobServices 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' existing = {
  parent: storageAccount
  name: 'default'
}

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [
  for containerName in containerNames: {
    parent: blobServices
    name: containerName
    properties: {
      publicAccess: publicAccess
    }
  }
]

@description('Names of the containers created.')
output containerNames array = [for (containerName, i) in containerNames: containers[i].name]
