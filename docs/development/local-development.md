
# Local Development

## Prerequisites

- Python 3.12+
- Azure Functions Core Tools v4
- Docker Desktop (for Container App services and local PostgreSQL/Service Bus emulators)
- Azurite (local Storage emulator) or an Azure Storage dev account
- `az` CLI, logged in with a dev-only identity, with the Bicep extension installed
  (`az bicep install`) — required for the local `bicep-lint` pre-commit hook

## Pre-commit hooks (do this first)

Run once, right after cloning:

```bash
pip install -r requirements-dev.txt --break-system-packages
pre-commit install
pre-commit install --hook-type commit-msg
```

From then on, every `git commit` runs black, ruff, detect-secrets, bicep-lint, general file
hygiene checks, and a commit-message format check — the commit is blocked until they pass.
Formatting/hygiene issues (black, ruff --fix, end-of-file-fixer, etc.) are fixed automatically
in your working tree; re-stage (`git add -u`) and commit again. See `CONTRIBUTING.md` for the
full hook list and what to do when a hook can't auto-fix something.

## Running a Function-based service (intake-processor, workflow-orchestrator, rule-engine, post-processing)

```bash
cd services/<service-name>
cp local.settings.json.example local.settings.json   # fill in local/dev values
pip install -r requirements.txt
func start
```

## Running a Container App service (di-processor, normalizer-service, audit-service)

```bash
cd services/<service-name>
cp .env.example .env
docker build -t <service-name>:local .
docker run --env-file .env -p 8080:8080 <service-name>:local
```

## Shared library

Services depend on `libs/dmer_common` as an editable local package during development:

```bash
pip install -e ../../libs/dmer_common
```

## Local messaging & storage

Use the Service Bus emulator (or a dedicated dev namespace) and Azurite for Blob Storage so
no local run touches shared dev/test resources.
