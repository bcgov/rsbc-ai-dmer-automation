
# workflow-orchestrator

See `docs/services/workflow-orchestrator.md` for full responsibilities and dependencies.

## Local run

```bash
cp local.settings.json.example local.settings.json
pip install -e ../../libs/dmer_common
pip install -r requirements.txt
func start
```

## Test

```bash
pytest tests/
```

## Deploy

Infrastructure is `infrastructure/bicep/workflow-orchestrator.bicep` with
`deployment/<env>/workflow-orchestrator.parameters.json`, deployed into the resource group
holding the pipeline's data resources (`rg-rsbc-dmer-dev` in DEV). Prerequisites it references
but doesn't create:

- the shared orchestrator subnet (`snet-rsbc-dmer-orchestrator-<env>`, delegated to
  `Microsoft.App/environments`, created with `modules/networking/subnet.bicep`);
- the `dmer-extracted` / `driver-decision` queues and the `extracted-dmer` / `normalized-dmer` /
  `rules` containers (`main.bicep`), and the active rule set at `rules/active/rules.json`;
- the deployment package container on the runtime storage account. Durable Functions keeps its
  state there in queues and tables as well as blobs, so that account needs blob, queue and table
  private endpoints.

```bash
az deployment group create -g rg-rsbc-dmer-dev \
  -f infrastructure/bicep/workflow-orchestrator.bicep \
  -p @deployment/dev/workflow-orchestrator.parameters.json \
  -p runtimeStorageConnectionString="$(az storage account show-connection-string -n rsbcstorage -g rsbc-dmer-ai-optimization-rg --query connectionString -o tsv)"
```

Then the Postgres role (once per environment): run `services/intake-processor/create-principal.sql`
against `postgres` and `roles.sql` against `dmer` with `function_app_name` set to this app's name
(`SET ROLE rsbc_dmer_admin` first, so the grants and default privileges cover the tables that
role owns).

Code: rebuild `dmer_common-0.1.0-py3-none-any.whl` from `libs/dmer_common` into this directory,
then `func azure functionapp publish <app> --python` (remote build).

**The `StartDocumentOrchestration` trigger deploys disabled** (`documentOrchestrationTriggerEnabled`
defaults to `false`, which sets `AzureWebJobs.StartDocumentOrchestration.Disabled=true` — the
host's own per-function switch, so the Service Bus listener never starts and messages wait on
`dmer-extracted` without consuming delivery attempts). To start processing, set the parameter to
`true` and redeploy, or flip the app setting (Portal: Functions → StartDocumentOrchestration →
Enable).
