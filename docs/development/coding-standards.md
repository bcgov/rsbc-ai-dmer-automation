
# Coding Standards

This project follows BC Government coding and delivery standards
(see `docs/standards/bc-gov-alignment.md`) plus the language/platform-specific rules below.

Formatting (`black`), linting (`ruff`), secret scanning (`detect-secrets`), Bicep linting, and
commit-message format are enforced automatically by pre-commit — see `CONTRIBUTING.md` and
`docs/development/local-development.md`. `mypy --strict` is intentionally left out of
pre-commit/CI for now (it needs each service's real dependencies installed, which don't exist
until business logic lands) — see the TODO in `.github/workflows/lint.yml` for wiring it up
per-service once that's true.

## Python (all Functions & Container Apps)

- Python 3.12, type-hinted, formatted with `black`, linted with `ruff`, type-checked with `mypy --strict`.
- One package per service under `src/<service_name>/`; no cross-service imports — shared code
  goes in `libs/dmer_common`.
- Structured logging only (no bare `print`); every log line includes `correlation_id`.
- All I/O (Blob, Service Bus, PostgreSQL, Document Intelligence, the external Azure OpenAI
  endpoint) goes through `libs/dmer_common` clients — no service opens a raw SDK/HTTP client
  directly.
- Config comes from environment variables sourced from App Configuration / Key Vault
  references; nothing is hardcoded per environment.

## Azure Functions

- Python v2 programming model (`function_app.py` decorators), one Function App per bounded context.
- Timer/HTTP/Service Bus triggers only call into `src/<service>/...` application code —
  triggers stay thin.
- Durable Functions: orchestrator functions must be deterministic (no direct I/O, no
  `datetime.now()`, no random) — all side effects happen in activity functions.

## Azure Container Apps

- Multi-stage Dockerfiles, non-root user, minimal base image (`python:3.12-slim`).
- Readiness/liveness probes required.
- Scaling rules defined in Bicep (KEDA Service Bus scaler), never hardcoded in the app.

## Bicep

- One resource type (or tightly related set) per module under `infrastructure/bicep/modules/`.
- Every module takes `environment`, `location`, and `tags` parameters at minimum.
- No secrets as plain parameters — use Key Vault references (`getSecret`) or deployment-time
  Managed Identity role assignments.
- Run `az bicep lint` and `az bicep build` before every PR (enforced by `bicep-validate.yml`).

## GitHub Actions

- Reusable/composite actions for anything duplicated across workflows.
- Least-privilege `permissions:` block on every workflow.
- OIDC federated credentials for Azure login — no long-lived service principal secrets.

## Managed Identity & networking

- Every service uses a **user-assigned Managed Identity**; no connection strings, no shared
  keys, no secrets in app settings other than Key Vault references — **except**
  `normalizer-service`'s call to the external Azure OpenAI endpoint (see below), which is
  the one documented exception in this repository.
- All PaaS dependencies we provision (Storage, Service Bus, PostgreSQL, Document Intelligence,
  Key Vault, App Configuration) are reached over **private endpoints** inside the platform
  team's VNet — public network access is disabled.
- The Azure OpenAI model `normalizer-service` calls is hosted in a separate Azure AI Hub/AI
  Foundry project in a separate subscription — not provisioned by this repo, not reachable via
  our private endpoints. It is called over its public endpoint, authenticated with an API key
  stored in Key Vault (read through `libs/dmer_common/config`), not Managed Identity. See
  `docs/architecture/repository-design.md` §10 and §13 (item 8).
