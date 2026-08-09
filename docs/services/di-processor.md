
# di-processor

**Azure compute:** Azure Container Apps (Service Bus-triggered via KEDA scaler)

## Responsibilities

- Subscribes to `raw-dmer-queue`.
- Downloads the DMER document from S3/URL.
- Calls Azure Document Intelligence over a private endpoint to perform OCR.
- Computes a SHA-256 hash of the source document for de-duplication.
- Persists document metadata + hash to PostgreSQL (`documents`, `document_hash`).
- Publishes the OCR result reference to `extracted-dmer-queue`; failures are dead-lettered.

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/di-processor/` for the required keys.
