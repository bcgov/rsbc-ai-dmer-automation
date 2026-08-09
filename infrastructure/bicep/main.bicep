// main.bicep
//
// Top-level orchestration template. Composes the modules under ./modules into the
// full DMER Automation environment (excluding manually-provisioned resources: resource
// groups, dev VMs, Document Intelligence custom model training, landing zone, VNet/
// subnets/flow logs/Network Watcher, and RBAC assignments beyond Managed Identity roles).
//
// NOTE: Azure OpenAI is NOT provisioned here. normalizer-service consumes a model hosted
// in a separate Azure AI Hub/AI Foundry project in a separate subscription via an
// endpoint URL + API key stored in Key Vault — see docs/architecture/repository-design.md
// §10 and §13 (item 8).
//
// Deployed per-environment with deployment/<env>/parameters.json — see
// docs/deployment/deployment-guide.md.
//
// Structural placeholder — module wiring intentionally deferred.
// See docs/architecture/repository-design.md for the full dependency diagram.

targetScope = 'resourceGroup'

@description('Target environment: dev | test | prod')
@allowed([
  'dev'
  'test'
  'prod'
])
param environment string

@description('Azure region')
param location string = resourceGroup().location

@description('Existing VNet resource ID (platform-provided, not created here)')
param vnetId string

@description('Standard resource tags')
param tags object = {
  project: 'dmer-automation'
  environment: environment
}

// TODO: module declarations wiring modules/* together, in dependency order:
// 1. monitor/log-analytics-workspace -> monitor/application-insights
// 2. identity/managed-identity (one per service)
// 3. keyvault/keyvault, appconfig/app-configuration
// 4. storage/storage-account -> storage/blob-containers
// 5. servicebus/namespace -> servicebus/queue, servicebus/topic
// 6. database/postgresql-flexible-server -> database/postgresql-database
// 7. ai/document-intelligence  (Azure OpenAI is external — not provisioned here)
// 8. networking/private-endpoint (per PaaS resource above)
// 9. compute/container-apps-environment -> compute/container-app (x3)
// 10. compute/function-app (x4)
// 11. monitor/diagnostic-settings (applied to all of the above), monitor/alerts
