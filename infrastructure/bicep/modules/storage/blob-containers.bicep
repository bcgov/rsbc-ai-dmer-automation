// blob-containers.bicep
//
// Purpose: Blob containers on an existing storage account. The full pipeline
// design calls for the stage containers — raw, ocr, normalized, rules, audit,
// failed, archive (docs/architecture/repository-design.md §11) — plus the two
// di-processor extraction-output containers, extracted-dmer and
// combined-extracted-dmer. Only the ones a currently-deployed consumer needs
// are created; the rest are added alongside the service that consumes them,
// via deployment/<env>/parameters.json (never hardcoded here).
//
// `raw` holds the Document Intelligence custom-model training/test document
// set. `extracted-dmer` (intermediate top-level/OCR/handwritten artifacts) and
// `combined-extracted-dmer` (the unified combined extraction) are consumed by
// di-processor. Note: those two names deliberately deviate from the
// lowercase-singular-noun container convention to match the pipeline stage
// names — see docs/standards/naming-conventions.md and the di-processor ADR.

@description('Name of the existing storage account to create containers on.')
param storageAccountName string

@description('Container names to create — lowercase, singular noun matching pipeline stage, except the di-processor extraction containers `extracted-dmer` and `combined-extracted-dmer` (stage-named exception). See docs/standards/naming-conventions.md.')
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
