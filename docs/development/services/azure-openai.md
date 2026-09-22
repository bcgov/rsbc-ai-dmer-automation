# Azure OpenAI (GPT-5.1, AI Hub)

Source: architecture doc §3.2, §4.2, §4.3.2, §9.1. Used by
[Extraction](../stages/02-extraction.md) (handwriting structuring) and
[Activity: Normalize](../stages/04-activity-normalize.md) (schema normalization).

## What this is

GPT-5.1, deployed in a **separate Azure AI Hub / AI Foundry project, in a separate Azure
subscription** — already deployed there, owned and managed outside this repository. Consumed
purely as an external HTTP dependency: an endpoint URL and API key, both stored as Key Vault
secrets in *this* subscription. This is the **one documented exception** to "managed identity
everywhere" in this project.

There is no `ai/openai.bicep` module and no Azure OpenAI entry in the naming convention or RBAC
list, by design — see `docs/architecture/repository-design.md` §10, §13 item 8. If the AI Hub
project ever moves into this subscription/tenant, or cross-subscription managed identity/Private
Link access is approved, introduce an `ai/openai.bicep` module and switch to managed identity at
that point — not before.

## Role in the pipeline

| Call site | Purpose |
|---|---|
| [Extraction](../stages/02-extraction.md) step 5 | GPT-5.1 structuring: turn OCR output from handwriting regions into structured JSON against a target schema, region by region. |
| [Activity: Normalize](../stages/04-activity-normalize.md) | Derive additional rule-ready fields from the handwritten extraction, validated against a schema and cross-checked against source text (evidence validation). |

Both call sites must **version their prompt/schema** and record the version on `dmer_stage_run.model_version`
(see `../data-model.md`) — a prompt change alters output, and a disputed decision months later will
turn on which version produced it.

## Network

Currently a **public endpoint with a key**. §9.1: hold the key in Key Vault with rotation, and plan
for a future private-endpoint move (no timeline given in the architecture document — treat this as
a known future migration, not an immediate task).

## Failure handling

Same transient/poison split as every external call: HTTP 429 or a transient error is retried with
backoff inside the activity; a schema-validation failure that can't be fixed by retrying is
poison — raise a terminal exception so the calling orchestration routes the document to
`MANUAL_REVIEW` (see `../stages/03-document-orchestration.md#failure-handling`).

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` | External AI Hub endpoint URL (App Configuration value). |
| `AZURE_OPENAI_API_KEY` | Key Vault secret — **never logged**. |
| `AZURE_OPENAI_DEPLOYMENT` | Deployment name. |
| `AZURE_OPENAI_API_VERSION` | Defaults to `2024-10-21` if unset. |

The env-var *names themselves* are overridable via `OPENAI_ENDPOINT_ENV`/`OPENAI_API_KEY_ENV`/
`OPENAI_DEPLOYMENT_ENV`/`OPENAI_API_VERSION_ENV` — see
`libs/dmer_common/src/dmer_common/config/__init__.py`.

## Authentication / identity

API key from Key Vault, read once via `dmer_common.config.openai_settings()` — the single read site
for this exception. **Never** attempt a managed-identity credential against this endpoint; it is
outside the tenant boundary and will simply fail auth.

## Security considerations

- No clinical content in Application Insights traces or exception messages — a stack trace that
  includes a prompt payload is a disclosure (§9.2). The `OpenAIClient.complete()` wrapper already
  logs only `model` and `message_count`, never message content or the API key — preserve this
  discipline in any new call site.
- Rotate the API key periodically; Key Vault rotation policy, not a code concern, but confirm the
  rotation cadence with whoever owns the AI Hub project.

## Monitoring

Alert on 429/5xx rate > 1% over 15 minutes from the external endpoint (§9.3) — this is a shared
threshold with Document Intelligence's quota alert; see `azure-monitor.md`.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/openai_client/client.py` (`OpenAIClient.complete()`) is
  reusable as-is for both call sites — already wraps retry (`openai_retry`: 4 attempts, 1–20s
  backoff) and a circuit breaker (`openai_breaker`), and is injectable with a mock SDK client for
  tests that assert no key is logged.
- `libs/dmer_common/src/dmer_common/config/__init__.py`'s `openai_settings()` is the only read site
  for the endpoint/key/deployment/api-version — don't add a second `os.environ` read anywhere else
  in the codebase for these values.

## Alignment gaps vs. current code

None — this is one of the few pieces of `dmer_common` that already matches the revised architecture
exactly (the "external dependency, key-based, single documented exception" model was already
correct under the original architecture and carries over unchanged). The only change is *which*
stages call it: originally only `normalizer-service`; now both
[Extraction](../stages/02-extraction.md) (handwriting structuring, new) and
[Activity: Normalize](../stages/04-activity-normalize.md) (schema normalization, same purpose as
before, different hosting — see that doc's alignment gap).
