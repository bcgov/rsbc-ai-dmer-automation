# Azure Container Apps

Source: architecture doc §3.2, §4.2. Bicep modules:
`infrastructure/bicep/modules/compute/{container-apps-environment,container-app}.bicep`.

## Role in the pipeline

**Exactly one Container App in the revised architecture**: the
[DMER Extraction Processor](../stages/02-extraction.md). Every other stage is either an Azure
Function (orchestration, timers, HTTP) or an in-process library call inside a Durable activity —
this is a deliberate reduction from the original architecture, which ran `di-processor`,
`normalizer-service`, and `audit-service` as three separate Container Apps. See
[Alignment gaps](#alignment-gaps-vs-current-code).

## Why Extraction is the one Container App

It runs for tens of seconds to minutes per document, loads Document Intelligence/OCR models, splits
page images, and scales on a completely different curve from the rest of the pipeline — bounded by
Document Intelligence and Azure OpenAI quota, not by cost. A Function App's execution time limits
and cold-start profile are a worse fit for this workload than a long-running, KEDA-scaled container.

## KEDA scaling

Scale rule: Service Bus queue depth on `dmer-raw`. **Max replicas bounded by Document Intelligence
quota**, not cost — over-scaling this component just produces more 429s from DI/OpenAI, not more
throughput. Min replicas 0–1 in dev (cold start acceptable), ≥1 in prod (extraction is on the
critical path; avoid cold start there).

## Environment

VNet-integrated Container Apps Environment, linked to the shared Log Analytics Workspace (see
`azure-monitor.md`). Private endpoints reach Blob, PostgreSQL, Document Intelligence from inside
the Container Apps Environment's VNet integration.

## Dockerfile

`services/di-processor/Dockerfile` exists as a placeholder — build the extraction processor's image
here. Base image should be a Python slim image matching the Python version `libs/dmer_common`
targets; confirm the shared library installs cleanly as a wheel/editable install inside the
container build (a monorepo path dependency, not a published package).

## Configuration / environment variables

Same as [Extraction §Configuration](../stages/02-extraction.md#configuration--environment-variables) —
Container Apps environment variables are populated from Azure App Configuration / Key Vault
references exactly like Function App settings; `dmer_common.config` reads them identically
regardless of compute host.

## Authentication / identity

User-assigned managed identity, RBAC-scoped to: Document Intelligence (`Cognitive Services User`),
Blob (`Storage Blob Data Contributor` on `raw-dmer`/`extracted-dmer`), Service Bus (`Data Receiver`
on `dmer-raw`, `Data Sender` on `dmer-extracted`), PostgreSQL (AAD role membership). Azure OpenAI is
the one exception — key-based via Key Vault, not managed identity (see `azure-openai.md`).

## Security considerations

Public network access disabled; private endpoints only, inside the platform VNet (§9.1). No
connection strings in Container App secrets — Key Vault references and managed identity only,
except the documented Azure OpenAI key exception.

## Implementation considerations for Claude Code

- `services/di-processor/src/di_processor/main.py` is currently
  `def main() -> None: raise NotImplementedError(...)` — this is the entrypoint to build out per
  [Extraction](../stages/02-extraction.md)'s nine processing steps.
- `services/di-processor/requirements.txt` is an empty placeholder — needs `azure-ai-documentintelligence`,
  `openai`, `azure-servicebus`, `azure-storage-blob`, `dmer_common` (editable/path install), and
  whatever image-segmentation library the page-segmentation step (step 3) uses.

## Alignment gaps vs. current code

`docs/architecture/repository-design.md` §3 lists three Container Apps: `di-processor`,
`normalizer-service`, `audit-service`. Under the revised architecture:

- `di-processor` remains a Container App, but its responsibilities change substantially (custom DI
  model + page segmentation + `prebuilt-ocr` + GPT-5.1 structuring + cut-off detection + driver
  resolution, replacing the older single-model-OCR-plus-hash flow) — see
  [Extraction](../stages/02-extraction.md#alignment-gaps-vs-current-code) is not itself flagged
  there since di-processor's compute type didn't change, only its internal steps; the gap is
  entirely in scope, not in hosting.
- `normalizer-service` is retired as a Container App — normalization becomes a Durable activity
  inside Document Orchestration (see [Activity: Normalize](../stages/04-activity-normalize.md#alignment-gaps-vs-current-code)).
- `audit-service` has no equivalent in the revised architecture at all — see
  `azure-database-postgresql.md#alignment-gaps-vs-current-code` and the top-level
  `../README.md#open-questions--decisions-required` for what to do with its placeholder folder.

Net effect: **one** Container App instead of three. Confirm this consolidation with the team before
deleting the `normalizer-service`/`audit-service` Container App Bicep instantiations (none exist yet
in `main.bicep`, so there's no infra to tear down — only the folders and old docs to reconcile).
