// queue.bicep
//
// Reusable Service Bus queue: max delivery count, lock duration, and an
// optional duplicate-detection window -- instantiated for dmer-ingest,
// dmer-raw and dmer-extracted (docs/development/message-contracts.md,
// revised architecture), each with its own settings taken from its contract.
//
// The native $DeadLetterQueue sub-queue always exists on a Service Bus
// queue regardless of deadLetteringOnMessageExpiration -- a message that
// exceeds maxDeliveryCount lands there automatically either way. That flag
// controls a *different* path: whether a message that simply expires (TTL
// elapsed, never redelivered enough times to hit maxDeliveryCount) also
// gets dead-lettered instead of silently vanishing. message-contracts.md
// requires this enabled on every queue ("applies to all four"), so it
// defaults to true here -- also newly applied to the two original-architecture
// queues built from this module, previously left at ARM's own default
// (false); enabling it is strictly additive (expired messages become
// visible in a DLQ instead of disappearing) so applies safely to those too.

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

@description('Duplicate-detection window (ISO 8601 duration), keyed on messageId. Empty string disables duplicate detection entirely -- use this when the contract doc doesn\'t call for it (e.g. dmer-raw).')
param duplicateDetectionWindow string = ''

@description('Whether a message that expires (TTL elapsed) without being redelivered enough times to hit maxDeliveryCount is dead-lettered instead of silently discarded. message-contracts.md requires this enabled on every queue.')
param deadLetteringOnMessageExpiration bool = true

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
    deadLetteringOnMessageExpiration: deadLetteringOnMessageExpiration
  }
}

output id string = queue.id
output name string = queue.name
