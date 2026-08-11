// blob-containers.bicep
//
// Purpose: Blob containers: raw, ocr, normalized, rules, audit, failed, archive
//
// Structural placeholder only — resource declarations are intentionally deferred.
// See docs/architecture/repository-design.md for this module's full responsibility,
// parameters, and dependency list.

@description('Target environment: dev | test | prod')
param environment string

@description('Azure region (Canada Central by default)')
param location string = resourceGroup().location

@description('Standard resource tags')
param tags object = {}



// TODO: resource declarations
