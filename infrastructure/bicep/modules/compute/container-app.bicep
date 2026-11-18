// container-app.bicep
//
// Purpose: Reusable Container App module for the DMER Container App services
// (image, ingress for health probes, user-assigned Managed Identity, a KEDA
// Service Bus scale rule, environment variables, optional Key Vault secret
// references, and optional diagnostic settings to Log Analytics).
//
// This module does NOT create the Container Apps Environment, the container
// registry, Service Bus, Key Vault, or Log Analytics — those are supplied by ID
// (the same "platform/shared resource passed in by ID" pattern main.bicep uses
// for the private-endpoint subnet and DNS zones). See
// docs/architecture/repository-design.md §11.
//
// Scaling is defined here (KEDA), never in the application, per
// docs/development/coding-standards.md#azure-container-apps.

@description('Container App resource name, e.g. ca-rsbc-dmer-di-proc-dev-001. Azure allows at most 32 characters, so the service part of the name must be short (di-processor is abbreviated to di-proc).')
@maxLength(32)
param name string

@description('Azure region (Canada Central by default).')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Resource ID of the Container Apps Environment this app runs in (platform/shared resource, passed in by ID).')
@minLength(1)
param containerAppsEnvironmentId string

@description('Resource ID of the user-assigned Managed Identity to attach (least-privilege RBAC assigned in main.bicep).')
@minLength(1)
param userAssignedIdentityId string

@description('Fully-qualified container image reference, e.g. myacr.azurecr.io/di-processor:1.0.0.')
@minLength(1)
param image string

@description('Optional container registry login server for a pull identity (e.g. myacr.azurecr.io). Empty = public image / no registry auth.')
param registryServer string = ''

@description('Container listening port for ingress health probes.')
param targetPort int = 8080

@description('Plain (non-secret) environment variables to inject, as name/value objects.')
param environmentVariables array = []

@description('Service Bus namespace FQDN the KEDA scaler and app connect to, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net.')
@minLength(1)
param serviceBusNamespaceFqdn string

@description('Service Bus queue the KEDA scaler watches (di-processor scales on dmer-raw depth).')
param scaleQueueName string = 'dmer-raw'

@description('KEDA target: scale out one replica per this many queued messages.')
param scaleMessageCount int = 5

@description('Minimum replicas. Keep >=1 in prod to avoid cold start on the critical path; 0 is acceptable in dev.')
@minValue(0)
param minReplicas int = 1

@description('Maximum replicas.')
@minValue(1)
param maxReplicas int = 10

@description('Optional Key Vault secret URI for the external Azure OpenAI API key (the one documented Managed-Identity exception). Empty = not wired here.')
param openAiApiKeySecretUri string = ''

@description('Optional Log Analytics Workspace resource ID for diagnostic settings. Empty = diagnostics not attached here.')
param logAnalyticsWorkspaceId string = ''

// CPU/memory sizing (Consumption profile defaults; override per environment).
@description('vCPU allocation for the container.')
param cpu string = '0.5'

@description('Memory allocation for the container.')
param memory string = '1Gi'

// Only attach the Key Vault secret + secretRef env var when a URI is supplied.
var openAiSecretName = 'azure-openai-api-key' // pragma: allowlist secret
var secrets = empty(openAiApiKeySecretUri)
  ? []
  : [
      {
        name: openAiSecretName
        keyVaultUrl: openAiApiKeySecretUri
        identity: userAssignedIdentityId
      }
    ]
var openAiEnv = empty(openAiApiKeySecretUri)
  ? []
  : [
      {
        name: 'AZURE_OPENAI_API_KEY'
        secretRef: openAiSecretName
      }
    ]

// KEDA authenticates to Service Bus with the attached Managed Identity — no
// connection-string secret. The identity goes INSIDE `custom` (a sibling of
// `metadata`); the service rejects it beside `custom` (ContainerAppInvalidSchema
// at `$[0]`), and API versions before 2025-01-01 reject it anywhere in the rule
// (verified with ARM preflight). `any()` keeps the shape if the bundled Bicep
// types lag behind the API.
var scaleRules = [
  any({
    name: 'servicebus-queue-depth'
    custom: {
      type: 'azure-servicebus'
      metadata: {
        queueName: scaleQueueName
        // KEDA wants the namespace NAME and appends .servicebus.windows.net
        // itself; passing the FQDN made it look up
        // "<ns>.servicebus.windows.net.servicebus.windows.net" (KEDAScalerFailed:
        // no such host), so the app never scaled up from zero.
        namespace: split(serviceBusNamespaceFqdn, '.')[0]
        messageCount: string(scaleMessageCount)
      }
      identity: userAssignedIdentityId
    }
  })
]

var registries = empty(registryServer)
  ? []
  : [
      {
        server: registryServer
        identity: userAssignedIdentityId
      }
    ]

resource containerApp 'Microsoft.App/containerApps@2025-01-01' = {
  name: name
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${userAssignedIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnvironmentId
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: secrets
      registries: registries
      ingress: {
        // Internal ingress: the health endpoints are reached by the platform
        // probes, not exposed publicly.
        external: false
        targetPort: targetPort
        transport: 'http'
        allowInsecure: false
      }
    }
    template: {
      containers: [
        {
          name: 'di-processor'
          image: image
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: concat(environmentVariables, openAiEnv)
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/healthz'
                port: targetPort
              }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/readyz'
                port: targetPort
              }
              initialDelaySeconds: 5
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: scaleRules
      }
    }
  }
}

// Optional diagnostic settings → shared Log Analytics Workspace: metrics only.
// A Container App has no resource-log categories (Azure rejects
// categoryGroup 'allLogs': "supported ones are: ''"); its console and system
// logs go to the workspace through the Container Apps environment's
// appLogsConfiguration (modules/compute/container-apps-environment.bicep).
resource diagnostics 'Microsoft.Insights/diagnosticSettings@2021-05-01-preview' = if (!empty(logAnalyticsWorkspaceId)) {
  name: '${name}-diag'
  scope: containerApp
  properties: {
    workspaceId: logAnalyticsWorkspaceId
    metrics: [
      {
        category: 'AllMetrics'
        enabled: true
      }
    ]
  }
}

@description('Resource ID of the Container App.')
output id string = containerApp.id

@description('Name of the Container App.')
output name string = containerApp.name

@description('Latest revision FQDN (internal ingress).')
output fqdn string = containerApp.properties.configuration.ingress.fqdn
