
# post-processing

**Azure compute:** Azure Functions (Service Bus topic-triggered)

## Responsibilities

- Subscribes to the `dmer-lifecycle-events` topic (`sub-post-processing`).
- Writes audit log entries (`audit_log`) for every processed DMER.
- Emits custom metrics to Application Insights (throughput, decision distribution,
  failure rate).
- Sends notifications (e.g. Teams/email) for failures or SLA breaches.
- Updates final document status/metadata.

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/post-processing/` for the required keys.
