# Azure Key Vault

Source: architecture doc §9.1. Bicep module: `infrastructure/bicep/modules/keyvault/keyvault.bicep`.

## Role

The only place secrets live. Two categories in this pipeline:

1. **Azure OpenAI endpoint + API key** — the one documented managed-identity exception (external
   AI Hub subscription; see `azure-openai.md`).
2. **Mercury credentials** — Mercury (Dynamics/OpenShift) is outside this tenant boundary, reached
   over ExpressRoute (question M-4, confirmed) with credential-based auth, not managed identity.

Everything else (Blob, Service Bus, PostgreSQL, Document Intelligence) uses managed identity and
has **no secret in Key Vault at all** — if you find yourself adding a connection string or key for
one of those, stop and use managed identity instead.

## Configuration

RBAC authorization mode (not access policies) — consistent, auditable permission grants alongside
every other resource's RBAC. Private endpoint. Soft-delete + purge protection enabled.

## Access pattern

App Configuration / Function App settings / Container App secrets resolve Key Vault references into
environment variables **at runtime** — application code never calls the Key Vault SDK directly.
`libs/dmer_common/src/dmer_common/config/__init__.py` is the single read site for these resolved
env vars (`require()`/`get()`, plus the typed `openai_settings()` helper) — see
`azure-openai.md#configuration--environment-variables`.

## Authentication / identity

`Key Vault Secrets User` (RBAC role) granted to each service's managed identity that needs a
secret — currently just Extraction/Normalize (OpenAI key) and Ingest/Driver Orchestration/Outbox
Publisher (Mercury credentials). No service needs `Key Vault Administrator` or broader access.

## Security considerations

- No secrets in source control, app settings, or Bicep parameters — Key Vault references only.
- `.gitignore` blocks `local.settings.json` and `.env` (local dev only, never committed).
- Rotate the Mercury credential and the Azure OpenAI API key on a defined cadence — not yet
  specified in the architecture document; confirm with whoever owns each external system.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/mercury_client/__init__.py` is currently a docstring-only stub —
  when building it out, read Mercury credentials via `dmer_common.config`, following the same
  single-read-site pattern `openai_settings()` already establishes; do not add a second
  `os.environ` read path for Mercury auth.
- `libs/dmer_common/src/dmer_common/auth/__init__.py` (the `DefaultAzureCredential` wrapper) is also
  a docstring-only stub and needs to be implemented before any managed-identity-authenticated client
  (Blob, Service Bus, PostgreSQL, Document Intelligence) can be wired up in a real Azure environment
  — `doc_intelligence/client.py` and `storage/client.py` currently instantiate
  `DefaultAzureCredential()` directly rather than going through this module; consider whether to
  route them through the shared wrapper once it exists, for consistent credential caching/logging
  across all four SDK clients.
