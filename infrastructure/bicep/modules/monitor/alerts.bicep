// alerts.bicep
//
// The pipeline's alert rules and their Action Group -- the table in
// docs/development/services/azure-monitor.md ("Metrics & alerts"), plus the
// infrastructure alerts kept from docs/operations/monitoring-alerts.md.
//
// Metric alerts (on the resources):
// - a dead letter on any pipeline queue; a queue backlog that keeps growing;
// - PostgreSQL CPU / storage;
// - Document Intelligence throttling (BlockedCalls: calls refused for rate
//   or quota).
//
// Log alerts (on the Log Analytics workspace every pipeline app reports to).
// The apps log one JSON object per line; it arrives as AppTraces.Message, so
// each query matches the line's "message" and reads its fields:
// - DLQ Drain: a dead letter needing human triage (UNKNOWN); one being
//   retried (TRANSIENT/PROCESSING);
// - Outbox Publisher: a Mercury write FAILED (once per row -- a person
//   resolves it in Mercury);
// - Reconciliation Sweeper: outbox backlog past SLA; a stuck driver
//   evaluation; a stalled document routed to manual review;
// - Daily reconciliation report: documents unaccounted for; the report not
//   run; manual reviews / fallback outcomes / stage p95 above their baseline;
// - Function invocation failures.
//
// The external Azure OpenAI gateway (AI Hub) is not ours to put a metric
// alert on, and normalization doesn't log its 429s, so its throttling shows
// only as normalization failures (manual reviews, stage p95).

@description('Target environment: dev | test | prod')
param environment string

@description('Region for the log alert rules (metric alerts and the action group are global).')
param location string = resourceGroup().location

@description('Standard resource tags')
param tags object = {}

@description('Prefix for the rule names, e.g. rsbc-dmer-dev.')
param namePrefix string

@description('Whether the rules are enabled.')
param enabled bool = true

@description('Email addresses the Action Group notifies. Empty: the rules fire into the portal only.')
param alertEmailAddresses array = []

@description('Resource ID of the Log Analytics workspace the pipeline\'s Application Insights components report to.')
param logAnalyticsWorkspaceId string

@description('Resource ID of the Service Bus namespace holding the pipeline queues.')
param serviceBusNamespaceId string

@description('Resource ID of the PostgreSQL Flexible Server.')
param postgresServerId string

@description('Resource ID of the Document Intelligence account.')
param documentIntelligenceId string

@description('Active messages on one queue, averaged over 30 minutes, that count as a stalled stage.')
param queueBacklogThreshold int = 500

@description('Documents routed to MANUAL_REVIEW in a day above which to alert (question I-16: agree a baseline).')
param manualReviewDailyThreshold int = 50

@description('Fallback outcomes (decided_by FALLBACK) queued in a day above which to alert.')
param fallbackDailyThreshold int = 20

@description('A stage\'s p95 duration in seconds over a day above which to alert.')
param stageP95SecondsThreshold int = 900

@description('Failed function invocations per app in 15 minutes above which to alert.')
param functionFailureThreshold int = 5

var actionGroupIds = [actionGroup.id]

resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: 'ag-${namePrefix}-pipeline'
  location: 'Global'
  tags: tags
  properties: {
    groupShortName: take('dmer${environment}', 12)
    enabled: true
    emailReceivers: [
      for (address, i) in alertEmailAddresses: {
        name: 'email-${i}'
        emailAddress: address
        useCommonAlertSchema: true
      }
    ]
  }
}

// ---------------------------------------------------------------------------
// Metric alerts
// ---------------------------------------------------------------------------
var allQueues = [
  {
    name: 'EntityName'
    operator: 'Include'
    values: [
      '*'
    ]
  }
]

var metricAlerts = [
  {
    name: 'dead-letter'
    description: 'A message was dead-lettered on a pipeline queue. The DLQ Drain records and routes it; this says one arrived.'
    severity: 2
    scope: serviceBusNamespaceId
    namespace: 'Microsoft.ServiceBus/namespaces'
    metric: 'DeadletteredMessages'
    aggregation: 'Maximum'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT5M'
    dimensions: allQueues
  }
  {
    name: 'queue-backlog'
    description: 'A pipeline queue has held many active messages for 30 minutes -- its stage is stalled or falling behind.'
    severity: 3
    scope: serviceBusNamespaceId
    namespace: 'Microsoft.ServiceBus/namespaces'
    metric: 'ActiveMessages'
    aggregation: 'Average'
    threshold: queueBacklogThreshold
    window: 'PT30M'
    frequency: 'PT5M'
    dimensions: allQueues
  }
  {
    name: 'postgres-cpu'
    description: 'PostgreSQL CPU above 80% for 15 minutes.'
    severity: 2
    scope: postgresServerId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'cpu_percent'
    aggregation: 'Average'
    threshold: 80
    window: 'PT15M'
    frequency: 'PT5M'
    dimensions: []
  }
  {
    name: 'postgres-storage'
    description: 'PostgreSQL storage above 80%.'
    severity: 2
    scope: postgresServerId
    namespace: 'Microsoft.DBforPostgreSQL/flexibleServers'
    metric: 'storage_percent'
    aggregation: 'Maximum'
    threshold: 80
    window: 'PT30M'
    frequency: 'PT15M'
    dimensions: []
  }
  {
    name: 'document-intelligence-throttled'
    description: 'Document Intelligence refused calls for rate or quota (429) -- the extraction throughput ceiling.'
    severity: 3
    scope: documentIntelligenceId
    namespace: 'Microsoft.CognitiveServices/accounts'
    metric: 'BlockedCalls'
    aggregation: 'Total'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT5M'
    dimensions: []
  }
]

resource metricAlertRules 'Microsoft.Insights/metricAlerts@2018-03-01' = [
  for alert in metricAlerts: {
    name: '${namePrefix}-${alert.name}'
    location: 'global'
    tags: tags
    properties: {
      description: alert.description
      severity: alert.severity
      enabled: enabled
      scopes: [
        alert.scope
      ]
      evaluationFrequency: alert.frequency
      windowSize: alert.window
      criteria: {
        'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
        allOf: [
          {
            name: alert.metric
            criterionType: 'StaticThresholdCriterion'
            metricName: alert.metric
            metricNamespace: alert.namespace
            operator: 'GreaterThan'
            threshold: alert.threshold
            timeAggregation: alert.aggregation
            dimensions: alert.dimensions
          }
        ]
      }
      autoMitigate: true
      actions: [
        for id in actionGroupIds: {
          actionGroupId: id
        }
      ]
    }
  }
]

// ---------------------------------------------------------------------------
// Log alerts -- one JSON log line per event, matched on its "message".
// ---------------------------------------------------------------------------
var drained = 'AppTraces | where Message has "dead letter drained" | extend j = parse_json(Message) | where tostring(j.message) == "dead letter drained"'
var report = 'AppTraces | where Message has "daily reconciliation report" | extend j = parse_json(Message) | where tostring(j.message) == "daily reconciliation report"'

var logAlerts = [
  {
    name: 'dlq-needs-triage'
    description: 'The DLQ Drain sent a document to MANUAL_REVIEW for a person: a logical error, an unknown reason, or retries used up. No decision was made.'
    severity: 2
    query: '${drained} | where tostring(j.failure_category) == "UNKNOWN"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'dlq-retrying'
    description: 'The DLQ Drain is retrying dead letters (TRANSIENT/PROCESSING): a dependency or our infrastructure is failing. A cluster means a systemic problem.'
    severity: 3
    query: '${drained} | where tostring(j.failure_category) in ("TRANSIENT", "PROCESSING")'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'outbox-write-failed'
    description: 'Mercury rejected an outcome write, or it ran out of attempts: the row is FAILED. A person resolves it in Mercury (docs/operations/runbook.md).'
    severity: 1
    query: 'AppTraces | where Message has "outbox row failed permanently" | extend j = parse_json(Message) | where tostring(j.message) == "outbox row failed permanently"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'outbox-backlog'
    description: 'Outcome writes are waiting past the SLA or being retried many times: Mercury is unavailable or rejecting. The no-lost-outcome guarantee is at risk.'
    severity: 1
    query: 'AppTraces | where Message has "mercury outbox backlog" | extend j = parse_json(Message) | where tostring(j.message) == "mercury outbox backlog"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'driver-evaluation-stuck'
    description: 'A driver evaluation was stuck in WAITING past the SLA after a sweeper pass (re-signalled, or not when its budget is used up).'
    severity: 3
    query: 'AppTraces | where Message has "stale driver evaluation" | extend j = parse_json(Message) | where tostring(j.message) startswith "stale driver evaluation"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'stalled-document-manual-review'
    description: 'The sweeper gave up re-publishing a stalled document and routed it to MANUAL_REVIEW.'
    severity: 2
    query: 'AppTraces | where Message has "stalled document routed to manual review" | extend j = parse_json(Message) | where tostring(j.message) == "stalled document routed to manual review"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
  {
    name: 'reconciliation-unaccounted'
    description: 'The daily reconciliation report found documents in Mercury\'s DPS queue not accounted for (or could not read the whole queue). The report is in the reconciliation-reports container.'
    severity: 1
    query: '${report} | where toint(j.unaccounted) > 0 or tostring(j.mercury_read_complete) == "false"'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT1H'
    frequency: 'PT1H'
  }
  {
    name: 'reconciliation-not-run'
    description: 'No daily reconciliation report in the last day -- the evidence the no-lost-outcome guarantee holds is missing.'
    severity: 2
    query: report
    operator: 'LessThan'
    threshold: 1
    window: 'P1D'
    frequency: 'PT6H'
  }
  {
    name: 'manual-review-rate'
    description: 'More documents went to MANUAL_REVIEW in a day than the baseline: a model or input-quality regression.'
    severity: 3
    query: '${report} | where toint(j.manual_review_24h) > ${manualReviewDailyThreshold}'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT1H'
    frequency: 'PT1H'
  }
  {
    name: 'fallback-rate'
    description: 'More fallback outcomes (AI could not process) were queued in a day than the baseline.'
    severity: 3
    query: '${report} | where toint(j.fallbacks_24h) > ${fallbackDailyThreshold}'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT1H'
    frequency: 'PT1H'
  }
  {
    name: 'stage-p95'
    description: 'A stage\'s p95 duration over the day is beyond the budget: capacity or quota pressure (including the external OpenAI gateway).'
    severity: 3
    query: '${report} | extend p = j.stage_p95_seconds | mv-expand bagexpansion=array p | where todouble(p[1]) > ${stageP95SecondsThreshold}'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT1H'
    frequency: 'PT1H'
  }
  {
    name: 'function-failures'
    description: 'Function invocations failing on a pipeline app.'
    severity: 2
    query: 'AppRequests | where Success == false | summarize failures = count() by AppRoleName | where failures > ${functionFailureThreshold}'
    operator: 'GreaterThan'
    threshold: 0
    window: 'PT15M'
    frequency: 'PT15M'
  }
]

resource logAlertRules 'Microsoft.Insights/scheduledQueryRules@2022-06-15' = [
  for alert in logAlerts: {
    name: '${namePrefix}-${alert.name}'
    location: location
    tags: tags
    properties: {
      displayName: '${namePrefix}-${alert.name}'
      description: alert.description
      severity: alert.severity
      enabled: enabled
      scopes: [
        logAnalyticsWorkspaceId
      ]
      evaluationFrequency: alert.frequency
      windowSize: alert.window
      criteria: {
        allOf: [
          {
            query: alert.query
            timeAggregation: 'Count'
            operator: alert.operator
            threshold: alert.threshold
            failingPeriods: {
              numberOfEvaluationPeriods: 1
              minFailingPeriodsToAlert: 1
            }
          }
        ]
      }
      autoMitigate: false
      actions: {
        actionGroups: actionGroupIds
      }
    }
  }
]

output actionGroupId string = actionGroup.id
