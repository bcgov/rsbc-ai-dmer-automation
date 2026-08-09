# RSBC DMER Optimization & Intake Automation

RSBC DMER optimization pilot for Ministry of AG/PSSG.

This repository is an enterprise monorepo for the **Driver Medical Examination Report (DMER) Intake Automation**
solution. It implements an AI-enabled pipeline that retrieves DMER documents from the Mercury (Dynamics) system,
performs automated document extraction (Azure Document Intelligence), normalization (Azure OpenAI, hosted in a
separate AI Hub subscription and consumed as an external endpoint — see
`docs/architecture/repository-design.md` §10), rule-based evaluation (GoRules/Zen), and writes decisions/audit
data back so Mercury remains the system of record.

## Start here

| I want to... | Go to |
|---|---|
| Understand the solution architecture | `docs/architecture/` |
| Understand a specific service's responsibilities | `docs/services/` |
| Understand queue/message contracts | `docs/contracts/queues/` |
| Set up a local dev environment | `docs/development/local-development.md` |
| Deploy infrastructure | `docs/deployment/deployment-guide.md` |
| Operate / troubleshoot production | `docs/operations/` |
| Follow coding standards | `docs/development/coding-standards.md`, `docs/standards/` |

## Repository layout

```
services/          Independently deployable services (Functions & Container Apps)
libs/               Shared Python libraries used across services
infrastructure/     Bicep IaC (modules + orchestration templates)
deployment/         Per-environment parameter files (dev/test/prod)
database/           PostgreSQL schema, migrations, seed data
monitoring/         Azure Monitor workbooks, alert definitions, dashboards
docs/               Architecture, service, contract, deployment, and ops documentation
scripts/            Developer, CI, and database utility scripts
.github/            GitHub Actions workflows and PR/issue templates
```

## Services

| Service | Azure compute | Responsibility |
|---|---|---|
| `intake-processor` | Azure Functions | Batch poll + webhook intake from Mercury, publish to `raw-dmer-queue` |
| `di-processor` | Azure Container Apps | OCR via Document Intelligence, hashing, publish to `extracted-dmer-queue` |
| `workflow-orchestrator` | Durable Functions | End-to-end orchestration: dedupe, case mgmt, normalization, rules, Mercury update |
| `normalizer-service` | Azure Container Apps | Structured extraction & evidence validation via an externally-hosted Azure OpenAI model |
| `rule-engine` | Azure Functions | GoRules/Zen rule evaluation against `rules.json` |
| `post-processing` | Azure Functions | Audit logging, metrics, notifications, final status |
| `audit-service` | Azure Container Apps | Read-side audit/query API for Mercury dashboard & operations |

See `docs/services/` for details on each.

## Status

Infrastructure and service skeletons only — see individual service READMEs for implementation status.
