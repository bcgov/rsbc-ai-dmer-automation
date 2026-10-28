// postgresql-database.bicep
//
// Purpose: The single database on the PostgreSQL Flexible Server (see
// postgresql-flexible-server.bicep) that the revised architecture's schema
// lives in — see database/migrations/V0001__create_dmer_pipeline_schema.sql
// and docs/development/data-model.md.
//
// Firewall rules aren't part of this module: the server's network access is
// entirely private-endpoint-based (publicNetworkAccess: Disabled), so
// Microsoft.DBforPostgreSQL/flexibleServers/firewallRules doesn't apply —
// there's no public IP allowlisting to configure. AAD administrator setup
// isn't here either — see postgresql-flexible-server.bicep's header for why
// that's confirmed not to exist as a formal ARM resource on the live server.
//
// The database's own schema (tables, columns) is a data-plane concern, not
// an ARM one — applied separately via schema.sql, same reasoning as
// Document Intelligence's labeling/training (see
// docs/deployment/deployment-guide.md, "Document Intelligence Studio,
// labeling, and custom models: what Bicep can and can't do").

@description('Name of the parent PostgreSQL Flexible Server.')
@minLength(3)
param serverName string

@description('Database name.')
param databaseName string = 'dmer'

resource server 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' existing = {
  name: serverName
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: server
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

@description('Name of the database.')
output name string = database.name
