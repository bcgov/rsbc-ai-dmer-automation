
# intake-processor

**Azure compute:** Azure Functions (Timer + HTTP triggers)

## Responsibilities

- Batch Poller (timer-triggered): calls the Mercury backlog API (DPS General & Unknown
  Queues) to retrieve batches of DMERs with metadata and a pre-signed URL (or reads
  directly from S3 and calls the Mercury GET API for metadata).
- Event Webhook Listener (HTTP-triggered): receives real-time new-DMER events from Mercury.
- Normalizes both intake paths into a single internal message contract and publishes to
  `raw-dmer-queue`.
- Owns all outbound Mercury read calls via the `mercury_client` shared library (anti-corruption layer).

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/intake-processor/` for the required keys.
