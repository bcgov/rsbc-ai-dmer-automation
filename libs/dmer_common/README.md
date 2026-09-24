
# dmer_common

Shared Python library used by every service in `services/`. Install as an editable local
package during development:

```bash
pip install -e ../../libs/dmer_common
```

## Modules

| Module | Purpose |
|---|---|
| `dto` | Shared message/data-transfer objects (queue envelopes, domain models) |
| `messaging` | Service Bus client wrapper, document ID (tracing) + idempotency helpers |
| `storage` | Blob Storage client + container path builders |
| `db` | PostgreSQL access layer, Managed Identity auth |
| `telemetry` | Structured logging + OpenTelemetry/App Insights setup |
| `auth` | Managed Identity / DefaultAzureCredential wrapper |
| `retry` | Standardized retry/circuit-breaker policies |
| `mercury_client` | Anti-corruption layer for Mercury (Dynamics) integration |
| `config` | App Configuration / Key Vault reference loader |
| `doc_intelligence` | Document Intelligence client (Managed Identity, private endpoint) |
| `openai_client` | External Azure OpenAI client (public endpoint, Key Vault API key) |

No service should import the raw Azure SDKs directly for these concerns — always go
through `dmer_common` so retry, auth, and telemetry behavior stay consistent.
