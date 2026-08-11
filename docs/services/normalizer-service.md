
# normalizer-service

**Azure compute:** Azure Container Apps

## Responsibilities

- Invoked by `workflow-orchestrator` as a durable activity (synchronous HTTPS call over
  the private VNet, Managed Identity-authenticated).
- Uses an Azure OpenAI model to perform section-based structured extraction against a strict
  per-section schema.
- Performs evidence validation — every extracted field must be traceable back to the
  original OCR text (span/quote match) — and produces a confidence score.
- Persists structured ontology output to PostgreSQL (`normalization_results`).

## External Azure OpenAI dependency

The OpenAI model used for normalization is hosted in a **separate Azure AI Hub/AI Foundry
project, in a separate Azure subscription** — it is already deployed there and is not
provisioned, versioned, or managed by this repository. This service calls it purely as an
external HTTPS dependency (public endpoint + API key), authenticated with a Key Vault secret
read via `libs/dmer_common/config` — **not** Managed Identity, since the target resource is
outside our subscription/tenant boundary. This is the one documented exception to this
project's "Managed Identity everywhere" rule. See
`docs/architecture/repository-design.md` §10 and §13 (item 8).

## Dependencies

See `docs/contracts/queues/` for the messages this service produces/consumes and
`docs/architecture/repository-design.md` for its Bicep module and Managed Identity role assignments.

## Configuration

Environment variables are sourced from Azure App Configuration and Key Vault references —
see `local.settings.json.example` (Functions) or `.env.example` (Container Apps) in this
service's folder under `services/normalizer-service/` for the required keys.
