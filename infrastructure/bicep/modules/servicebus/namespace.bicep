// namespace.bicep
//
// Service Bus namespace. Premium tier -- required for a private endpoint
// (Basic/Standard don't support one at all), and this landing zone's
// Deny-PublicPaaSEndpoints policy blocks a publicly-reachable namespace
// outright, so Premium is the only tier actually deployable here, not a
// preference -- see the what-if error this was validated against:
// "RequestDisallowedByPolicy ... prevent public IP addresses on the target
// Azure PaaS service(s)" when Standard + public access was tried first.

@description('Service Bus namespace name.')
@minLength(6)
@maxLength(50)
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Namespace SKU. Premium is required in this landing zone -- see this file\'s header.')
@allowed([
  'Basic'
  'Standard'
  'Premium'
])
param skuName string = 'Premium'

@description('Number of messaging units for Premium (1, 2, 4, 8, or 16 -- ignored for Basic/Standard). Namespace-wide capacity shared by every queue/topic inside it, not per-entity -- see docs/services capacity note. Starting at the minimum; raise only if actual throughput needs it.')
@allowed([
  1
  2
  4
  8
  16
])
param premiumMessagingUnits int = 1

@description('Whether the namespace is reachable over its public endpoint. Disabled once a private endpoint is in place -- matches every other PaaS resource in this repo.')
@allowed([
  'Enabled'
  'Disabled'
])
param publicNetworkAccess string = 'Disabled'

@description('Availability-zone redundancy (Premium only). Off by default for DEV -- a prod-hardening concern, not needed for this environment\'s Automatic/no-approval deploy policy.')
param zoneRedundant bool = false

@description('Disables local (SAS key) auth, forcing Azure AD/RBAC only -- matches docs/standards/security-guidelines.md\'s default of no key-based auth where avoidable.')
param disableLocalAuth bool = true

resource namespace 'Microsoft.ServiceBus/namespaces@2024-01-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuName
    capacity: skuName == 'Premium' ? premiumMessagingUnits : null
  }
  properties: {
    disableLocalAuth: disableLocalAuth
    publicNetworkAccess: publicNetworkAccess
    zoneRedundant: zoneRedundant
  }
}

output id string = namespace.id
output name string = namespace.name
output serviceBusEndpoint string = namespace.properties.serviceBusEndpoint

@description('Namespace host name, e.g. sb-rsbc-dmer-shared-dev-001.servicebus.windows.net -- what SDK clients and the KEDA scaler take as the fully-qualified namespace.')
output fullyQualifiedNamespace string = split(replace(namespace.properties.serviceBusEndpoint, 'https://', ''), ':')[0]
