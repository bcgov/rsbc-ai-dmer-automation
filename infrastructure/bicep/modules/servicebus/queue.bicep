// queue.bicep
//
// Reusable Service Bus queue: max delivery count, lock duration, and an
// optional duplicate-detection window -- instantiated for raw-dmer-queue
// and extracted-dmer-queue, each with its own settings taken from its
// contract doc (docs/contracts/queues/*.md).
//
// Dead-lettering needs no property to enable: the native $DeadLetterQueue
// sub-queue always exists on a Service Bus queue, and a message that
// exceeds maxDeliveryCount lands there automatically.

@description('Service Bus namespace this queue belongs to (name, not resource ID).')
@minLength(1)
param namespaceName string

@description('Queue name -- kebab-case, `<domain>-<stage>-queue`, see docs/standards/naming-conventions.md.')
@minLength(1)
param name string

@description('Times a message is delivered before being dead-lettered.')
@minValue(1)
@maxValue(2000)
param maxDeliveryCount int = 5

@description('How long a receiver holds a lock on a message before it becomes available to another receiver again (ISO 8601 duration).')
param lockDuration string = 'PT5M'

@description('Duplicate-detection window (ISO 8601 duration), keyed on messageId. Empty string disables duplicate detection entirely -- use this when the contract doc doesn\'t call for it (e.g. extracted-dmer-queue).')
param duplicateDetectionWindow string = ''

resource sbNamespace 'Microsoft.ServiceBus/namespaces@2024-01-01' existing = {
  name: namespaceName
}

resource queue 'Microsoft.ServiceBus/namespaces/queues@2024-01-01' = {
  parent: sbNamespace
  name: name
  properties: {
    maxDeliveryCount: maxDeliveryCount
    lockDuration: lockDuration
    requiresDuplicateDetection: !empty(duplicateDetectionWindow)
    duplicateDetectionHistoryTimeWindow: empty(duplicateDetectionWindow) ? 'PT10M' : duplicateDetectionWindow
  }
}

output id string = queue.id
output name string = queue.name
