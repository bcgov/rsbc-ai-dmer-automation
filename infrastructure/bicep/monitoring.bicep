// monitoring.bicep
//
// Resource-group-scoped entry point for the pipeline's alert rules and their
// Action Group (modules/monitor/alerts.bicep; the alert catalogue is
// docs/development/services/azure-monitor.md "Metrics & alerts"). Deployed
// into the resource group holding the pipeline's data resources
// (rg-rsbc-dmer-dev in DEV); it references, never creates, the Service Bus
// namespace, the PostgreSQL server, the Document Intelligence account and the
// Log Analytics workspace the apps' Application Insights components report to.

targetScope = 'resourceGroup'

import { buildTags } from 'modules/shared/tags.bicep'

@description('Target environment: dev | test | prod')
@allowed([
  'dev'
  'test'
  'prod'
])
param environment string

@description('Azure region for the log alert rules. BC Gov landing zone workloads are Canada-only.')
@allowed([
  'canadacentral'
  'canadaeast'
])
param location string = 'canadacentral'

@description('Whether the alert rules are enabled.')
param alertsEnabled bool = true

@description('Email addresses notified when an alert fires. Empty: alerts show in the portal only.')
param alertEmailAddresses array = []

@description('Resource ID of the Log Analytics workspace the pipeline apps report to.')
@minLength(1)
param logAnalyticsWorkspaceId string

@description('Resource ID of the Service Bus namespace holding the pipeline queues.')
@minLength(1)
param serviceBusNamespaceId string

@description('Resource ID of the PostgreSQL Flexible Server.')
@minLength(1)
param postgresServerId string

@description('Resource ID of the Document Intelligence account.')
@minLength(1)
param documentIntelligenceId string

@description('Active messages on one queue, averaged over 30 minutes, that count as a stalled stage.')
param queueBacklogThreshold int = 500

@description('Daily MANUAL_REVIEW count above which to alert (question I-16: agree a baseline).')
param manualReviewDailyThreshold int = 50

@description('Daily fallback-outcome count above which to alert.')
param fallbackDailyThreshold int = 20

@description('Stage p95 duration (seconds, over a day) above which to alert.')
param stageP95SecondsThreshold int = 900

@description('Failed function invocations per app in 15 minutes above which to alert.')
param functionFailureThreshold int = 5

@description('Cost center tag value.')
param costCenter string = 'RSBC'

@description('Owner tag value (team or distribution list).')
param owner string = 'RSBC-DMER'

@description('Data classification tag value.')
param dataClassification string = 'protected-b'

module alerts 'modules/monitor/alerts.bicep' = {
  name: '${deployment().name}-alerts'
  params: {
    environment: environment
    location: location
    tags: buildTags(environment, 'monitoring', costCenter, owner, dataClassification)
    namePrefix: 'rsbc-dmer-${environment}'
    enabled: alertsEnabled
    alertEmailAddresses: alertEmailAddresses
    logAnalyticsWorkspaceId: logAnalyticsWorkspaceId
    serviceBusNamespaceId: serviceBusNamespaceId
    postgresServerId: postgresServerId
    documentIntelligenceId: documentIntelligenceId
    queueBacklogThreshold: queueBacklogThreshold
    manualReviewDailyThreshold: manualReviewDailyThreshold
    fallbackDailyThreshold: fallbackDailyThreshold
    stageP95SecondsThreshold: stageP95SecondsThreshold
    functionFailureThreshold: functionFailureThreshold
  }
}

output actionGroupId string = alerts.outputs.actionGroupId
