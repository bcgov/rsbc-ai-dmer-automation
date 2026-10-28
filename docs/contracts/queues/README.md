# Queue & Topic Contracts

One file per Service Bus entity documenting: purpose, producer(s), consumer(s), message
schema, partition/session key (if any), retry/max-delivery-count, and dead-letter handling.
See `extracted-dmer-queue.md` and `dmer-lifecycle-events-topic.md`.

`raw-dmer-queue.md` was removed — it described the original architecture's two-queue shape,
now superseded by the revised architecture's four queues; see
`../../development/message-contracts.md`.
