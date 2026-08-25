# DMER Intake Automation — Repository & Infrastructure Design

**Source of truth:** `docs/architecture/DMER Intake Automation- Architecture (Azure Cloud) - Explained.docx`

This document is the enterprise repository and infrastructure design for the DMER Optimization /
Intake Automation project: a monorepo following Azure best practices, Domain-Driven service
boundaries, clean architecture within each service, GitOps delivery, and Bicep-based
Infrastructure as Code. It expands on the architecture document into a concrete, buildable
repository — the physical skeleton for everything described here has already been created
under this repository root.

---

## 1. High-Level Repository Structure

```
rsbc-ai-dmer-automation/
├── .github/
│   ├── workflows/
│   ├── ISSUE_TEMPLATE/
│   ├── CODEOWNERS
│   └── pull_request_template.md
├── docs/
│   ├── architecture/
│   ├── deployment/
│   ├── development/
│   ├── services/
│   ├── contracts/
│   │   ├── queues/
│   │   └── api/
│   ├── operations/
│   └── standards/
├── infrastructure/
│   └── bicep/
│       ├── main.bicep
│       ├── modules/
│       └── README.md
├── deployment/
│   ├── dev/
│   ├── test/
│   └── prod/
├── services/
│   ├── intake-processor/
│   ├── di-processor/
│   ├── workflow-orchestrator/
│   ├── normalizer-service/
│   ├── rule-engine/
│   ├── post-processing/
│   └── audit-service/
├── libs/
│   └── dmer_common/
├── database/
│   ├── schema/
│   ├── migrations/
│   └── seed/
├── monitoring/
│   ├── dashboards/
│   ├── alerts/
│   └── workbooks/
├── scripts/
│   ├── dev/
│   ├── ci/
│   └── db/
├── .bin/                  # pre-commit local-hook scripts (e.g. bicep-lint.sh)
├── .editorconfig
├── .gitignore
├── .pre-commit-config.yaml
├── .secrets.baseline      # detect-secrets baseline, committed intentionally
├── CONTRIBUTING.md
├── README.md
├── requirements-dev.txt   # pre-commit/black/ruff/detect-secrets — dev/CI tooling only
└── LICENSE
```

## 2. Explanation of Each Top-Level Folder


| Folder                  | Purpose                                                                                                                                                                                          |
| ----------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `.github/`              | CI/CD workflows, PR/issue templates, code ownership — the GitOps control surface for this repo.                                                                                                  |
| `docs/`                 | All documentation: architecture, per-service responsibilities, message/API contracts, deployment, operations, and coding standards.                                                              |
| `infrastructure/bicep/` | Infrastructure-as-Code templates and reusable modules only — **no environment-specific values**.                                                                                                 |
| `deployment/`           | Per-environment (`dev`/`test`/`prod`) Bicep parameter files and deployment notes — keeps `infrastructure/` free of duplication.                                                                  |
| `services/`             | One folder per independently buildable/deployable service (Azure Functions or Container Apps).                                                                                                   |
| `libs/`                 | Shared Python libraries consumed by every service (`dmer_common`) — the only place cross-cutting concerns (messaging, storage, DB, auth, retry, telemetry, Mercury integration) are implemented. |
| `database/`             | PostgreSQL schema reference, forward-only migrations, and non-PII dev seed data.                                                                                                                 |
| `monitoring/`           | Dashboards, workbook exports, and the human-readable alert catalogue that backs `infrastructure/bicep/modules/monitor/alerts.bicep`.                                                             |
| `scripts/`              | Developer bootstrap scripts, CI/CD helper scripts, and DB migration/seed tooling.                                                                                                                |
| `.bin/`                 | Wrapper scripts for pre-commit's `local` hooks (e.g. `bicep-lint.sh`).                                                            |


## 3. Service Responsibilities

Service boundaries were derived directly from the "Components in detail" section of the
architecture document, with three adjustments explained in §13 (an added `audit-service`, a
`mercury_client` shared library rather than a standalone service, and Azure OpenAI treated as an
external dependency rather than a resource this repo provisions).


| Service                   | Azure compute        | Trigger                                                            | Responsibilities                                                                                                                                                                                                                                                                                                                                                                      |
| ------------------------- | -------------------- | ------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **intake-processor**      | Azure Functions      | Timer (Batch Poller) + HTTP (Webhook Listener)                     | Retrieves backlog DMERs from Mercury's DPS General/Unknown Queues (via API + pre-signed URL, or S3 + Mercury GET), and receives real-time new-DMER events. Publishes a normalized message to `raw-dmer-queue`.                                                                                                                                                                        |
| **di-processor**          | Azure Container Apps | Service Bus (`raw-dmer-queue`), KEDA-scaled                        | Downloads the DMER from S3/URL, calls Azure Document Intelligence (private endpoint) for OCR, computes a SHA-256 hash, persists metadata + hash to PostgreSQL, publishes to `extracted-dmer-queue`. Failures → DLQ.                                                                                                                                                                   |
| **workflow-orchestrator** | Durable Functions    | Service Bus (`extracted-dmer-queue`) starts the orchestration      | Coordinates, as durable activities: metadata loading, duplicate validation, case creation/update in Mercury, normalization invocation, rule evaluation invocation, Mercury system update, completion marking, retry, and failure routing. Publishes lifecycle events to the `dmer-lifecycle-events` topic.                                                                            |
| **normalizer-service**    | Azure Container Apps | HTTPS activity call from workflow-orchestrator                     | Uses an Azure OpenAI model — hosted in a separate Azure AI Hub/AI Foundry project in a separate subscription, consumed via endpoint URL + API key only (see §10, §13) — for section-based structured extraction against a strict per-section schema, evidence validation against the original OCR text, confidence scoring, and persistence of the structured ontology to PostgreSQL. |
| **rule-engine**           | Azure Functions      | HTTPS activity call from workflow-orchestrator                     | Loads `rules.json` from Blob Storage using GoRules/Zen, evaluates the normalized ontology, produces a decision and reason codes, persists the evaluation snapshot and rule version.                                                                                                                                                                                                   |
| **post-processing**       | Azure Functions      | Service Bus topic (`dmer-lifecycle-events`, `sub-post-processing`) | Audit logging, custom metrics, notifications, final metadata/status update.                                                                                                                                                                                                                                                                                                           |
| **audit-service**         | Azure Container Apps | HTTPS (read API)                                                   | Read-only query API over the audit/history tables for Mercury's Review Dashboard and operational tooling — deliberately separated from `post-processing`'s write path (see §13).                                                                                                                                                                                                      |


Each service is independently buildable/deployable: its own `requirements.txt`/`pyproject.toml`,
its own Dockerfile (Container Apps) or `host.json` (Functions), its own unit/integration test
suites, and no cross-service imports — only `libs/dmer_common` is shared.

## 4. Shared Library Design (`libs/dmer_common`)


| Module           | Responsibility                                                                                                                                                                                                                                                          |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `dto`            | Pydantic message/data-transfer objects — the single source of truth for the schemas documented in `docs/contracts/queues/`.                                                                                                                                             |
| `messaging`      | Service Bus client wrapper: standard envelope, correlation ID propagation, idempotency helpers keyed on `messageId`.                                                                                                                                                    |
| `storage`        | Blob Storage client (Managed Identity auth) + path builders for the container layout in §on Storage below.                                                                                                                                                              |
| `db`             | PostgreSQL access layer (SQLAlchemy/asyncpg), connection pooling, AAD/Managed Identity token auth, repository base classes.                                                                                                                                             |
| `telemetry`      | Structured JSON logging with automatic correlation ID injection and PII redaction; OpenTelemetry/Application Insights wiring.                                                                                                                                           |
| `auth`           | `DefaultAzureCredential`/Managed Identity token acquisition, shared by every Azure SDK client.                                                                                                                                                                          |
| `retry`          | Tenacity-based retry policies and circuit breakers, standardized across Mercury, Document Intelligence, OpenAI, and Service Bus calls.                                                                                                                                  |
| `mercury_client` | **Anti-corruption layer** for Mercury (Dynamics): typed client for batch/backlog retrieval, webhook payload parsing, and case update calls — isolates Mercury's data shapes from internal domain models so a Mercury API change touches one module, not seven services. |
| `config`         | App Configuration / Key Vault reference loader — the only place environment configuration is read. Also the only place `normalizer-service` reads the external AI Hub OpenAI endpoint URL + API key (Key Vault secret) from — see §10, §13.                             |


Rationale: every cross-cutting concern that would otherwise be copy-pasted per service (and drift)
lives here instead. No service opens a raw Azure SDK client directly for Storage, Service Bus,
PostgreSQL, or Mercury.

## 5. Infrastructure Folder Layout

```
infrastructure/
└── bicep/
    ├── main.bicep              # orchestration template, composes all modules
    ├── README.md
    └── modules/
        ├── networking/
        ├── identity/
        ├── storage/
        ├── servicebus/
        ├── database/
        ├── keyvault/
        ├── appconfig/
        ├── monitor/
        ├── ai/
        ├── compute/
        └── shared/
```

`infrastructure/bicep` holds templates only — no environment-specific parameter values. Those
live in `deployment/<env>/parameters.json` (§7), so the same templates deploy dev, test, and
prod without duplication.

## 6. Complete Bicep Folder Structure

```
infrastructure/bicep/
├── main.bicep              # resource-group-scoped workload template
├── subscription.bicep      # subscription-scoped entry point: creates the resource group
│                            # and private-endpoint subnet, then invokes main.bicep
├── README.md
└── modules/
    ├── networking/
    │   ├── private-endpoint.bicep
    │   ├── subnet.bicep                    # private-endpoint subnet inside the existing VNet
    │   └── network-security-group.bicep    # baseline NSG for that subnet
    ├── identity/
    │   └── managed-identity.bicep
    ├── storage/
    │   ├── storage-account.bicep
    │   └── blob-containers.bicep
    ├── servicebus/
    │   ├── namespace.bicep
    │   ├── queue.bicep
    │   └── topic.bicep
    ├── database/
    │   ├── postgresql-flexible-server.bicep
    │   └── postgresql-database.bicep
    ├── keyvault/
    │   └── keyvault.bicep
    ├── appconfig/
    │   └── app-configuration.bicep
    ├── monitor/
    │   ├── log-analytics-workspace.bicep
    │   ├── application-insights.bicep
    │   ├── diagnostic-settings.bicep
    │   └── alerts.bicep
    ├── ai/
    │   └── document-intelligence.bicep
    ├── compute/
    │   ├── container-apps-environment.bicep
    │   ├── container-app.bicep
    │   └── function-app.bicep
    └── shared/
        ├── naming.bicep
        ├── tags.bicep
        └── resource-group.bicep    # subscription-scoped; used by subscription.bicep only
```

## 7. Deployment Folder Structure

```
deployment/
├── dev/
│   ├── parameters.json
│   └── README.md
├── test/
│   ├── parameters.json
│   └── README.md
└── prod/
    ├── parameters.json
    └── README.md
```

Each `parameters.json` supplies `environment`, `location`, and standard tags, plus the values
`infrastructure/bicep/subscription.bicep` needs to create the resource group and
private-endpoint subnet: `resourceGroupName`, `vnetResourceGroupName`, `vnetName` (the
platform-provided VNet — referenced, not created), and `privateEndpointSubnetAddressPrefix`.
SKUs, scaling limits, and any other environment-specific value belong here, never hardcoded in
a module. See `docs/deployment/deployment-guide.md` for the full parameter list and how to
verify platform-owned values (VNet name, Private DNS zones, RBAC) before filling in the
placeholders these files ship with.

## 8. GitHub Actions Folder Structure

```
.github/
├── workflows/
│   ├── build.yml              # build/package every changed service
│   ├── test.yml                # unit + integration tests, coverage gates
│   ├── lint.yml                 # ruff/black/mypy + bicep lint
│   ├── bicep-validate.yml       # az bicep build + what-if
│   ├── security-scan.yml        # pip-audit, Trivy, gitleaks
│   ├── pr-validation.yml        # aggregates required checks for branch protection
│   ├── deploy-dev.yml           # auto-deploy on merge to main
│   ├── deploy-test.yml          # manual promotion, 1 approver
│   └── deploy-prod.yml          # manual promotion, 2 approvers + change window
├── ISSUE_TEMPLATE/
│   ├── bug_report.md
│   └── feature_request.md
├── CODEOWNERS
└── pull_request_template.md
```

Build/test/lint/security-scan run on every PR and are required status checks (branch protection
on `main`). Deploy workflows are environment-gated: dev is continuous, test/prod require
GitHub Environment approvals — this is the GitOps promotion path (§14).

## 9. Documentation Folder Structure

```
docs/
├── architecture/
│   ├── DMER Intake Automation- Architecture (Azure Cloud) - Explained.docx
│   ├── repository-design.md        # this document
│   ├── solution-architecture.md
│   └── decision-records/           # ADRs, one file per decision
├── deployment/
│   ├── deployment-guide.md
│   ├── environment-setup.md
│   └── rollback-procedures.md
├── development/
│   ├── local-development.md
│   ├── coding-standards.md
│   └── testing-guide.md
├── services/
│   └── <one file per service>.md
├── contracts/
│   ├── queues/
│   │   ├── raw-dmer-queue.md
│   │   ├── extracted-dmer-queue.md
│   │   └── dmer-lifecycle-events-topic.md
│   └── api/                        # OpenAPI specs per HTTP surface
├── operations/
│   ├── runbook.md
│   ├── troubleshooting.md
│   ├── monitoring-alerts.md
│   └── incident-response.md
└── standards/
    ├── bc-gov-alignment.md
    ├── naming-conventions.md
    └── security-guidelines.md
```

## 10. Bicep Module List with Responsibilities


| Module                                      | Responsibility                                                                                                                                                                                                                                                              |
| ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `networking/private-endpoint.bicep`         | Reusable private endpoint + private DNS zone group, parameterized by target resource ID and group ID — used by every PaaS module below.                                                                                                                                     |
| `networking/subnet.bicep`                   | Creates the private-endpoint subnet inside the *existing* platform VNet. Deployed scoped to the VNet's own resource group (platform-owned), not the application resource group — see `subscription.bicep`.                                                                |
| `networking/network-security-group.bicep`   | Baseline NSG for that subnet. Optional — an existing platform-managed NSG can be supplied instead via `subscription.bicep`'s `existingNetworkSecurityGroupId` parameter.                                                                                                    |
| `shared/resource-group.bicep`               | Creates the application resource group (e.g. `rg-rsbc-dmer-dev`). Subscription-scoped; only used by `subscription.bicep`, never by `main.bicep`.                                                                                                                            |
| `identity/managed-identity.bicep`           | User-assigned Managed Identity per service, with an optional RBAC role-assignment block (least privilege only).                                                                                                                                                             |
| `storage/storage-account.bicep`             | StorageV2 account: private endpoint, TLS 1.2 minimum, public network access disabled, lifecycle management policy hook.                                                                                                                                                     |
| `storage/blob-containers.bicep`             | Creates the seven containers: `raw`, `ocr`, `normalized`, `rules`, `audit`, `failed`, `archive` (§ Storage layout).                                                                                                                                                         |
| `servicebus/namespace.bicep`                | Service Bus namespace, Premium tier (for private endpoint + availability zone support), private endpoint.                                                                                                                                                                   |
| `servicebus/queue.bicep`                    | Reusable queue module: max delivery count, lock duration, duplicate-detection window — instantiated for `raw-dmer-queue` and `extracted-dmer-queue`.                                                                                                                        |
| `servicebus/topic.bicep`                    | Reusable topic + subscription module — instantiated for `dmer-lifecycle-events`.                                                                                                                                                                                            |
| `database/postgresql-flexible-server.bicep` | PostgreSQL Flexible Server: VNet-integrated, Azure AD authentication enabled, zone-redundant HA in prod.                                                                                                                                                                    |
| `database/postgresql-database.bicep`        | Logical database + AAD administrator + firewall configuration.                                                                                                                                                                                                              |
| `keyvault/keyvault.bicep`                   | Key Vault: RBAC authorization mode, private endpoint, soft-delete + purge protection.                                                                                                                                                                                       |
| `appconfig/app-configuration.bicep`         | Azure App Configuration store, with Key Vault reference support for secrets.                                                                                                                                                                                                |
| `monitor/log-analytics-workspace.bicep`     | Single shared Log Analytics Workspace for the environment.                                                                                                                                                                                                                  |
| `monitor/application-insights.bicep`        | Workspace-based Application Insights, linked to the Log Analytics Workspace.                                                                                                                                                                                                |
| `monitor/diagnostic-settings.bicep`         | Reusable diagnostic settings module, applied to every resource created by the other modules.                                                                                                                                                                                |
| `monitor/alerts.bicep`                      | Metric/log alert rules (DLQ depth, Function failure rate, Container App restarts, PostgreSQL CPU/storage, DI throttling, external OpenAI endpoint throttling) + Action Group.                                                                                               |
| `ai/document-intelligence.bicep`            | Document Intelligence (Cognitive Services) account: private endpoint, public network access disabled. Custom model training remains manual (§13).                                                                                                                           |
| `compute/container-apps-environment.bicep`  | VNet-integrated Container Apps Environment, linked to the Log Analytics Workspace.                                                                                                                                                                                          |
| `compute/container-app.bicep`               | Reusable Container App module: image, KEDA Service Bus scale rule, Managed Identity, ingress config — instantiated for `di-processor`, `normalizer-service`, `audit-service`.                                                                                               |
| `compute/function-app.bicep`                | Reusable Function App module: Premium/Elastic Premium plan (required for VNet integration + no cold start on the orchestrator), Managed Identity, App Insights connection — instantiated for `intake-processor`, `workflow-orchestrator`, `rule-engine`, `post-processing`. |
| `shared/naming.bicep`                       | Naming-convention helper functions (generates a resource name from type/service/env/region/instance — see §12).                                                                                                                                                             |
| `shared/tags.bicep`                         | Standard tag set helper (`environment`, `service`, `costCenter`, `owner`, `dataClassification`).                                                                                                                                                                            |


**Note on Azure OpenAI:** there is no `ai/openai.bicep` module, and no Azure OpenAI resource is
provisioned by this repository. The model `normalizer-service` calls is hosted in a **separate
Azure AI Hub / AI Foundry project, in a separate Azure subscription**, owned and managed outside
this repo — the model is already deployed there and out of scope for our Bicep and our RBAC.
`normalizer-service` consumes it purely as an external HTTP dependency: an endpoint URL and an
API key, both stored as Key Vault secrets in *this* subscription and read at runtime through
`libs/dmer_common/config` (see §4 and §13, item 8). If that ever changes — e.g. the project is
folded into this subscription, or cross-subscription Managed Identity/Private Link access is
approved — introduce an `ai/openai.bicep` module (account + optionally a `deployment`
sub-resource) and switch `normalizer-service` from key-based auth to Managed Identity.

## 11. Azure Resource Dependency Diagram

```
Log Analytics Workspace
 └─ Application Insights
 └─ Diagnostic Settings ── attached to every resource below

Managed Identities (one per service)
 └─ RBAC role assignments →
      Storage Account (Blob Data Contributor/Reader as appropriate)
      Service Bus Namespace (Data Sender/Receiver per queue/topic)
      PostgreSQL Flexible Server (AAD role membership)
      Key Vault (Key Vault Secrets User via RBAC)
      App Configuration (App Configuration Data Reader)
      Document Intelligence (Cognitive Services User)
      (normalizer-service additionally reads a Key Vault secret for the external
       AI Hub OpenAI endpoint + API key — no RBAC role assignment, no local resource)

Existing VNet (platform-provided, referenced only — never created by this repository)
 └─ Subnet: private-endpoint subnet (created by subscription.bicep, scoped to the VNet's own
    resource group) + NSG (created alongside it, or an existing platform-managed one)
      └─ Private Endpoints →
           Storage Account (blob)
           Service Bus Namespace
           PostgreSQL Flexible Server
           Key Vault
           App Configuration
           Document Intelligence
      └─ Container Apps Environment (VNet-integrated)
      └─ Function App VNet integration (Premium plan, per Function App)

Storage Account
 └─ Blob Containers: raw, ocr, normalized, rules, audit, failed, archive

Service Bus Namespace
 └─ Queue: raw-dmer-queue (+ native DLQ)
 └─ Queue: extracted-dmer-queue (+ native DLQ)
 └─ Topic: dmer-lifecycle-events
      └─ Subscription: sub-post-processing (+ native DLQ)
      └─ Subscription: sub-audit-service (+ native DLQ)

PostgreSQL Flexible Server
 └─ Database: dmer

Key Vault
 └─ Secrets (fallback/non-MI credentials only, if any)

App Configuration
 └─ Key-values (feature flags, non-secret config, Key Vault references)

Document Intelligence account   (custom model training: manual, out of Bicep)

[external] Azure OpenAI endpoint (separate AI Hub subscription — not provisioned by this repo;
           reached over the public endpoint using a Key Vault-stored API key; see §10, §13)

Container Apps Environment
 ├─ Container App: di-processor          → consumes raw-dmer-queue, calls Document Intelligence
 ├─ Container App: normalizer-service    → calls the [external] Azure OpenAI endpoint, writes PostgreSQL
 └─ Container App: audit-service         → reads PostgreSQL

Function Apps (Premium plan, VNet-integrated)
 ├─ intake-processor        → calls Mercury, publishes raw-dmer-queue
 ├─ workflow-orchestrator   → consumes extracted-dmer-queue, calls normalizer-service +
 │                             rule-engine, calls Mercury, publishes dmer-lifecycle-events
 ├─ rule-engine              → reads rules/ blob container, writes PostgreSQL
 └─ post-processing          → consumes dmer-lifecycle-events (sub-post-processing),
                                writes PostgreSQL, writes failed/ blob container

Monitor
 └─ Alerts + Action Group ← metrics/logs from every resource above via Diagnostic Settings
```

**Deployment order implied by the diagram:** Resource Group → private-endpoint Subnet/NSG (both
via `subscription.bicep`, subscription-scoped) → Log Analytics → Managed Identities → Key Vault /
App Configuration → Storage → Service Bus → PostgreSQL → Document Intelligence →
Private Endpoints → Container Apps Environment → Container Apps → Function Apps → Diagnostic
Settings/Alerts. `main.bicep` should express this via natural resource dependencies (implicit in
Bicep through property references), not manual `dependsOn` where avoidable. The external Azure
OpenAI endpoint has no deployment ordering dependency here — it's a runtime config value
(Key Vault secret), not a resource this template creates or waits on.

## 12. Recommended Naming Conventions

Pattern (Cloud Adoption Framework-aligned):

```
<resource-type-abbreviation>-rsbc-dmer-<service-or-shared>-<environment>-<instance>
```

- `<environment>`: `dev` | `test` | `prod`
- `<instance>`: zero-padded 3-digit counter, `001`, `002`, ...


| Resource                   | Abbreviation | Example                                                |
| -------------------------- | ------------ | ------------------------------------------------------ |
| Resource Group              | `rg`         | `rg-rsbc-dmer-prod` *(created by `subscription.bicep`, subscription-scoped)* |
| Subnet                      | `snet`       | `snet-rsbc-dmer-pe-prod-001`                           |
| Network Security Group      | `nsg`        | `nsg-rsbc-dmer-pe-prod-001`                             |
| Storage Account            | `st`         | `stdmerprodcac001` *(no dashes, ≤24 chars, lowercase)* |
| Service Bus Namespace      | `sb`         | `sb-rsbc-dmer-shared-prod-001`                         |
| PostgreSQL Flexible Server | `psql`       | `psql-rsbc-dmer-shared-prod-001`                       |
| Key Vault                  | `kv`         | `kv-rsbc-dmer-shared-prod-001` *(≤24 chars)*           |
| App Configuration          | `appcs`      | `appcs-rsbc-dmer-shared-prod-001`                      |
| Container Apps Environment | `cae`        | `cae-rsbc-dmer-shared-prod-001`                        |
| Container App              | `ca`         | `ca-rsbc-di-processor-prod-001`                        |
| Function App               | `func`       | `func-rsbc-intake-processor-prod-001`                  |
| Application Insights       | `appi`       | `appi-rsbc-dmer-shared-prod-001`                       |
| Log Analytics Workspace    | `log`        | `log-rsbc-dmer-shared-prod-001`                        |
| Managed Identity           | `id`         | `id-rsbc-dmer-di-processor-prod-001`                   |
| Document Intelligence      | `di`         | `di-rsbc-dmer-shared-prod-001`                         |
| Private Endpoint           | `pe`         | `pe-psql-rsbc-dmer-shared-prod-001`                    |


*(No `oai` entry — Azure OpenAI is not a resource this repository provisions; see §10, §13.)*

Service Bus entities: `kebab-case`, `<domain>-<stage>-queue` for queues (`raw-dmer-queue`),
`<domain>-events` for topics (`dmer-lifecycle-events`), `sub-<consumer>` for subscriptions
(`sub-post-processing`). Blob containers: lowercase singular noun matching pipeline stage
(`raw`, `ocr`, `normalized`, `rules`, `audit`, `failed`, `archive`). PostgreSQL: `snake_case`,
plural table names (except `audit_log`, kept singular by log-table convention).

## 13. Recommended Additions Beyond the Architecture Document

The architecture document is complete for the happy-path pipeline; the following were added or
adjusted to make the repository production-ready:

1. `**audit-service` (new service).** The document says auditing is written by Post Processing
  and the Review Dashboard is implemented *inside Mercury*. Rather than having Mercury (or ops
   tooling) query PostgreSQL directly, a small read-only API service gives a controlled,
   independently-scalable, independently-secured (read-only Managed Identity) boundary for
   external consumers — a standard CQRS read/write split.
2. `**mercury_client` as a shared library, not a service.** Both `intake-processor` (reads) and
  `workflow-orchestrator` (writes/updates) talk to Mercury. Rather than a separate "Mercury
   integration service" (which would just proxy calls and add a network hop), this is modeled as
   an anti-corruption-layer module in `libs/dmer_common` — same isolation benefit, no extra
   deployable surface.
3. `**dmer-lifecycle-events` topic (new messaging construct).** The document doesn't specify how
  `post-processing` learns a case is complete. A topic (rather than a direct call or a queue)
   was chosen because more than one downstream consumer plausibly needs the same "completed/
   failed" event (`post-processing` today, `audit-service` optionally, a future notification
   service later) — a queue would force a single consumer or manual fan-out.
4. `**dead_letter_events` table + `failed/` blob container.** The document names a dead-letter
  queue but not what happens to a dead-lettered message operationally. Persisting DLQ payload +
   metadata gives ops a triage surface without requiring Service Bus Explorer access to prod.
5. **Azure API Management — recommended, not yet scaffolded.** If `intake-processor`'s webhook
  endpoint or `audit-service`'s read API will be called by anything outside the VNet (e.g. a
   Mercury plugin, an external ops tool), put APIM in front for auth, rate limiting, and a stable
   contract surface independent of the underlying compute. Not included in the Bicep module list
   above because it's conditional on that external-caller requirement being confirmed — add
   `infrastructure/bicep/modules/apim/` when it is.
6. `**rules` versioning (`rules/active/` vs `rules/versions/<version>/`).** The document says
  `rules.json` is read from Blob Storage but doesn't describe change management. Versioned
   storage plus an `active` pointer lets a rule change be tested against golden files and rolled
   back by pointer swap rather than redeploying the Rule Engine.
7. **STRA/PIA alignment note.** DMER content is personal medical information subject to FOIPPA.
  This isn't a new component, but it drives concrete Bicep defaults (public network access
   disabled everywhere, Canadian regions only, encryption at rest) — captured in
   `docs/standards/bc-gov-alignment.md` and `docs/standards/security-guidelines.md`.
8. **Azure OpenAI is an external dependency, not a provisioned resource.** The normalization
  model runs in a separate Azure AI Hub/AI Foundry project, in a separate Azure subscription,
   that's already deployed and managed outside this repository — we don't own it, don't deploy
   its model, and don't control its networking. Consequently there is no `ai/openai.bicep`
   module and no Azure OpenAI entry in the naming convention or RBAC list. `normalizer-service`
   authenticates with an endpoint URL + API key pulled from Key Vault at runtime
   (`libs/dmer_common/config`) rather than a cross-subscription Managed Identity role assignment.
   This is a deliberate, documented exception to the "Managed Identity everywhere, zero secrets"
   rule in §14 — revisit it if the AI Hub project ever moves into this subscription/tenant
   boundary, at which point Private Link + Managed Identity becomes possible.

## 14. Best Practices & Recommendations

### Scalability

- Container Apps scale on Service Bus queue/subscription depth via KEDA scale rules (min replicas
0–1 in dev, ≥1 in prod to avoid cold start on the pipeline's critical path).
- Function Apps use a Premium/Elastic Premium plan (required for VNet integration and to avoid
Consumption-plan cold starts on `workflow-orchestrator`, which is latency-sensitive as the
pipeline's coordination point).
- PostgreSQL Flexible Server sized per environment in `deployment/<env>/parameters.json`; enable
read replicas only if `audit-service` read load later contends with the write path.
- Service Bus Premium tier for predictable throughput and private endpoint support at scale.

### Security

- Managed Identity everywhere **except** the external Azure AI Hub OpenAI endpoint
(`normalizer-service` only), which is outside this subscription and authenticates with a
Key Vault-stored API key instead — a documented, single exception (§13, item 8), not a pattern
to extend to other dependencies.
- Zero connection strings/shared keys in app settings or Bicep
parameters for everything else.
- Public network access disabled on every PaaS resource; private endpoints only, inside the
platform-provided VNet.
- Key Vault RBAC authorization mode (not access policies) for consistent, auditable permission
grants alongside every other resource's RBAC.
- PII redaction at the logging layer (`libs/dmer_common/telemetry`) — never rely on downstream
log scrubbing.
- Dependency, container, and secret scanning on every PR (`security-scan.yml`).
- Canadian regions only for data residency; encryption at rest (platform default) and TLS 1.2+
in transit.

### Monitoring

- One shared Log Analytics Workspace per environment; every resource's Diagnostic Settings point
to it — avoids fragmented logs across per-resource workspaces.
- Correlation ID (Mercury case ID or generated UUID) propagated end-to-end through the message
envelope so a single Application Insights query can trace one DMER through all seven services.
- Alert on leading indicators (DLQ depth, throttling rate) as well as lagging ones (failure rate),
since a growing DLQ predicts a future failure spike.
- Workbooks for rule-decision distribution and normalization confidence trend — these are
business-outcome signals, not just infrastructure health.

### Resiliency

- Service Bus native retry (max delivery count) + dead-lettering at every queue/subscription;
`post-processing` captures DLQ payloads to Blob + PostgreSQL for triage rather than leaving them
opaque in Service Bus.
- Idempotency by `messageId` at every consumer — safe redelivery and safe DLQ redrive.
- Durable Functions checkpointing gives `workflow-orchestrator` resumability across process
restarts without custom state management.
- Circuit breakers (via `libs/dmer_common/retry`) around Mercury, Document Intelligence, and
OpenAI calls so an external outage degrades gracefully (messages queue up) instead of cascading.
- Database migrations follow expand/contract so a service rollback never forces a schema rollback.

### GitOps

- `main` is the only deployable branch; environment promotion is dev (automatic) → test (1
approver) → prod (2 approvers + change window), enforced via GitHub Environments, not manual
process.
- Infrastructure and application code are versioned and reviewed together in this monorepo — no
infrastructure drift from out-of-band portal changes (`az deployment group what-if` in CI
catches drift before every deploy).
- Bicep templates are the single source of truth; parameters are the only thing that varies per
environment, and they're reviewed via the same PR process as code.
- CODEOWNERS ensures the platform team reviews any change to `infrastructure/`, `database/`, and
shared `libs/`, while each service's owning team reviews its own folder.
- `pre-commit` (`.pre-commit-config.yaml`) enforces formatting (black), linting (ruff), secret
scanning (detect-secrets against `.secrets.baseline`), Bicep linting, general file hygiene, and
Conventional Commits message format *before* a commit is even created — and the identical hook
set runs again in CI (`lint.yml`) against the full repo, so `--no-verify` can't be used to slip
something past review that a clean checkout would also fail. See `CONTRIBUTING.md`.

---

*This document should be updated in the same PR as any change to service boundaries, message
contracts, or the Bicep module list — see `CONTRIBUTING.md`.*
