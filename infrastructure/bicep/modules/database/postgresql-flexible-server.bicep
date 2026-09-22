// postgresql-flexible-server.bicep
//
// Purpose: PostgreSQL Flexible Server backing dmer_processing/mercury_links
// (see services/intake-processor/schema.sql). Written to capture
// rsbc-ai-database-dev's actual live configuration, provisioned by hand
// before this module existed.
//
// Network model: private endpoint (publicNetworkAccess: Disabled), not the
// alternative VNet-delegated-subnet model Postgres Flexible Server also
// supports — matches every other PaaS resource in this repo, which all use
// the private-endpoint pattern (see modules/networking/private-endpoint.bicep).
// The private endpoint itself is deployed separately by whatever composes
// this module (see main.bicep's pattern for Document Intelligence/Storage),
// not created here.
//
// AAD authentication capability is enabled (authConfig.activeDirectoryAuth),
// but no formal AAD administrator is configured via the
// Microsoft.DBforPostgreSQL/flexibleServers/administrators sub-resource —
// deliberately not managed by this template, since it's tied to whichever
// person bootstraps a given environment, not a fixed identity. This is a
// genuine prerequisite, not optional: confirmed by testing against a fresh
// server that the password-authenticated admin login alone cannot bootstrap
// per-service AAD roles (e.g. intake-processor's managed identity,
// POSTGRES_USER = <function-app-name>) — pgaadauth_create_principal applies
// a Postgres SECURITY LABEL that only a connection already mapped to a
// Microsoft Entra principal is allowed to apply. Add yourself as the
// server's Microsoft Entra Administrator first (Server -> Settings ->
// Authentication, or `az postgres flexible-server microsoft-entra-admin
// create`), then see services/intake-processor/create-principal.sql +
// roles.sql (run via apply_roles.sh) for the out-of-band SQL bootstrap
// itself — see docs/services/intake-processor.md.

@description('Server name, e.g. rsbc-ai-database-dev.')
param name string

@description('Azure region.')
param location string = resourceGroup().location

@description('Standard resource tags.')
param tags object = {}

@description('Administrator login username.')
@minLength(1)
param administratorLogin string

@secure()
@description('Administrator login password. Supply via a pipeline secret, never a parameters.json file.')
param administratorLoginPassword string

@description('PostgreSQL major version.')
param postgresVersion string = '18'

@description('Compute SKU name, e.g. Standard_B2s.')
param skuName string = 'Standard_B2s'

@description('Compute SKU tier.')
param skuTier string = 'Burstable'

@description('Storage size in GB.')
param storageSizeGB int = 32

@description('Backup retention in days.')
param backupRetentionDays int = 7

@description('Availability zone.')
param availabilityZone string = '2'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: name
  location: location
  tags: tags
  sku: {
    name: skuName
    tier: skuTier
  }
  properties: {
    version: postgresVersion
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorLoginPassword
    authConfig: {
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Enabled'
    }
    storage: {
      storageSizeGB: storageSizeGB
      autoGrow: 'Disabled'
    }
    backup: {
      backupRetentionDays: backupRetentionDays
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Disabled'
    }
    availabilityZone: availabilityZone
  }
}

@description('Resource ID of the server.')
output id string = server.id

@description('Name of the server.')
output name string = server.name

@description('Fully-qualified domain name, e.g. rsbc-ai-database-dev.postgres.database.azure.com — pass to intake-processor.bicep\'s postgresHost parameter.')
output fullyQualifiedDomainName string = server.properties.fullyQualifiedDomainName
