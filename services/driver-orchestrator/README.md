# driver-orchestrator

Stage 4 of the revised architecture: the per-driver join. Decides the outcome
of every waiting DMER of a driver, once per driver at a time. See
`docs/development/stages/06-driver-orchestration.md` and `07-decision-gateway.md`;
business rules follow architecture v2.1 §4.4.3 (the Sep 29, 2026 Intake
sign-off).

## How it runs

`DriverDecision` is a Service Bus trigger on `driver-decision` with **sessions
enabled** (`SessionId = driver_key`, set by Document Orchestration's Signal
Driver). The Functions host processes one driver's messages one at a time,
each to completion, so a signal never races another for the same driver.
Each signal runs `evaluate.evaluate_driver`:

1. Take the driver's advisory lock and find its open `driver_evaluation`.
2. Load the batch (documents counted into it) with extraction values and the
   latest rule evaluation.
3. Re-check Mercury `GET by driver_licence`; check completeness.
4. Not complete: record `WAITING` (reason, Mercury check time, refreshed
   count) and stop.
5. Complete: `decision.decide()`, then `DECIDED` + one `dmer_decision` row per
   document, in one transaction, and the batch is **closed** -- the driver's
   next DMER starts a new batch (I-10; migration V0009). If the Rule Engine
   counted another DMER in meanwhile, nothing is written and the signal is
   retried with the full batch.

> **Pending architect approval:** the architecture describes a Durable
> orchestration per driver. This app uses a session-triggered function
> instead (a Durable starter holds the session lock only while starting the
> orchestration, so a second signal for the same driver could be lost).
> `evaluate_driver` would be the Durable activity unchanged.

| Module | What it does |
|---|---|
| `decision/` | Pure outcome logic (`decide`) and the completeness check |
| `batch.py` | Database rows → `decide()` input |
| `evaluate.py` | One evaluation of one driver (database + Mercury + decision) |
| `function_app.py` | The `DriverDecision` session-triggered function |
| `dmer_common.db.driver_decision` | The database unit of work (lock, batch, status, decisions) |

Not built yet: the hand-off to Post-Processing.

## Deploy

Infrastructure is `infrastructure/bicep/driver-orchestrator.bicep` with
`deployment/<env>/driver-orchestrator.parameters.json`, deployed into the resource group
holding the pipeline's data resources (`rg-rsbc-dmer-dev` in DEV). Prerequisites it references
but doesn't create:

- the shared orchestrator subnet (`snet-rsbc-dmer-orchestrator-<env>`), the same one the
  Document Orchestration app uses;
- the session-enabled `driver-decision` queue (`main.bicep`);
- the deployment package container on the runtime storage account
  (`app-package-func-rsbc-dmer-driver-orchestrator-dev` in DEV). Not a Durable Functions app, so
  no queue or table state is kept there.

The `DriverDecision` trigger deploys **disabled** (`driverDecisionTriggerEnabled = false`), so
signals wait on `driver-decision` until it is switched on.

```bash
az deployment group create -g rg-rsbc-dmer-dev   -f infrastructure/bicep/driver-orchestrator.bicep   -p @deployment/dev/driver-orchestrator.parameters.json   -p runtimeStorageConnectionString="$(az storage account show-connection-string -n rsbcstorage -g rsbc-dmer-ai-optimization-rg --query connectionString -o tsv)"
```

Then the Postgres role (once per environment): run `services/intake-processor/create-principal.sql`
against `postgres` and `roles.sql` against `dmer` with `function_app_name` set to this app's name
(`SET ROLE rsbc_dmer_admin` first, so the grants and default privileges cover the tables that
role owns). The app reads `driver`, `dmer_document`, `dmer_extraction`, `rule_evaluation` and
`driver_evaluation_document`, and writes `driver_evaluation`, `dmer_document`, `dmer_stage_run`
and `dmer_decision`.

Code: rebuild `dmer_common-0.1.0-py3-none-any.whl` from `libs/dmer_common` into this directory,
then `func azure functionapp publish <app> --python` (remote build).

## Local run

```bash
cp local.settings.json.example local.settings.json
pip install -e ../../libs/dmer_common
pip install -r requirements.txt
func start
```

## Test

```bash
pip install -e ".[dev]"
pytest tests/unit
# Integration tests need a disposable PostgreSQL (all migrations are applied):
NORMALIZATION_TEST_DSN=postgresql+asyncpg://postgres@localhost:55432/postgres pytest tests
```
