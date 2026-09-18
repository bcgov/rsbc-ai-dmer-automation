// data-plane-role-assignment.bicep
//
// Purpose: Grants a principal (typically a VM/Function App's managed
// identity) a Service Bus data-plane RBAC role, scoped to an existing
// namespace that may live in a different resource group than whatever is
// deploying this — a separate module (not a raw resource declaration) is
// required for that cross-resource-group scoping; see
// https://aka.ms/bicep/core-diagnostics#BCP139.
//
// The role assignment's own `name` is a deterministic GUID built from
// static inputs only (namespace name + principal id + role id) rather than
// from a module output that's only known after deployment — Azure
// requires resource names to be calculable at the start of deployment
// (see https://aka.ms/bicep/core-diagnostics#BCP120), which a managed
// identity's principalId, generated at VM/Function App creation time,
// is not.

@description('Name of the existing Service Bus namespace.')
@minLength(1)
param serviceBusNamespaceName string

@description('Principal ID (object ID) of the identity to grant the role to.')
@minLength(1)
param principalId string

@description('Principal type — ServicePrincipal for a managed identity.')
param principalType string = 'ServicePrincipal'

@description('Role definition GUID to assign, e.g. Azure Service Bus Data Receiver (4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0) or Azure Service Bus Data Sender (69a216fc-b8fb-44d8-bc22-1f3c2cd27a39).')
@minLength(1)
param roleDefinitionId string

resource serviceBusNamespace 'Microsoft.ServiceBus/namespaces@2022-10-01-preview' existing = {
  name: serviceBusNamespaceName
}

resource roleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(serviceBusNamespace.id, principalId, roleDefinitionId)
  scope: serviceBusNamespace
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', roleDefinitionId)
    principalId: principalId
    principalType: principalType
  }
}
