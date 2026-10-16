# reliability

The reliability components of the revised architecture (v2.1 §4.6): timer functions that make
sure a decided DMER's outcome actually reaches Mercury. See
`docs/development/stages/09-reliability-components.md`.

Today it hosts the **Outbox Publisher**. The Reconciliation Sweeper and the DLQ Drain will join
it as further timer functions.

## Outbox Publisher

Post-Processing (inside the Driver Orchestration's decision transaction, and Driver Lookup's
I-12/I-22 route) writes one `mercury_outbox` row per Mercury operation, in the same transaction
as the decision. `OutboxPublisher` drains them about once a minute
(`OUTBOX_PUBLISHER_SCHEDULE`). The logic is in `dmer_common.db.outbox_publisher.publish_due`:

1. claim up to `OUTBOX_BATCH_SIZE` documents with a due `PENDING` row, leasing their rows
   (`OUTBOX_LEASE_SECONDS`) so a crashed run's rows come back rather than being lost;
2. send each document's rows in order -- `MAP_DRIVER`, `CREATE_CASE`, `MARK_DUPLICATE`,
   `UPDATE_OUTCOME` -- with the row's idempotency key; a failure stops that document so the
   outcome is never written before its driver or case;
3. record `SENT`, or back off exponentially (`OUTBOX_BACKOFF_INITIAL_SECONDS` doubling to
   `OUTBOX_BACKOFF_MAX_SECONDS`), or `FAILED` when Mercury rejects the write (a 4xx) or after
   `OUTBOX_MAX_ATTEMPTS` -- logged at ERROR for the alert; the document's later rows fail as
   "blocked";
4. a document whose rows are all `SENT` goes `POSTING` -> `COMPLETED`, and its driver
   evaluation `DECIDED` -> `POSTED` once the whole batch is sent. Fallback outcomes for documents
   in `MANUAL_REVIEW` leave their status alone.

**Mercury's write API is provisional** (question M-6: Mercury hasn't shared its POST/PUT
structure or idempotency behaviour). `MercuryClient.send_outbox` and the dev mock
(`services/intake-processor/tests/local/mock-mercury-function-app`) implement the assumed
endpoints; only those two should need to change once M-6 is answered. The mock records every
write under `mock-writes/<document_guid>/` and can simulate an outage with
`MOCK_WRITE_FAIL_STATUS` (e.g. `503`).

Logs carry ids, operations, idempotency keys and HTTP statuses only -- payloads carry clinical
content and licences.

## Deploy

Infrastructure is `infrastructure/bicep/reliability.bicep` with
`deployment/<env>/reliability.parameters.json`, deployed into the resource group holding the
pipeline's data resources (`rg-rsbc-dmer-dev` in DEV). Prerequisites it references but doesn't
create:

- the shared orchestrator subnet (`snet-rsbc-dmer-orchestrator-<env>`);
- the deployment package container on the runtime storage account
  (`app-package-func-rsbc-dmer-reliability-dev` in DEV).

The `OutboxPublisher` timer deploys **disabled** (`outboxPublisherEnabled = false`), so outbox rows
wait as `PENDING` until it is switched on.

```bash
az deployment group create -g rg-rsbc-dmer-dev   -f infrastructure/bicep/reliability.bicep   -p @deployment/dev/reliability.parameters.json   -p runtimeStorageConnectionString="$(az storage account show-connection-string -n rsbcstorage -g rsbc-dmer-ai-optimization-rg --query connectionString -o tsv)"   -p mercuryApiKey="$(az functionapp config appsettings list -g rg-rsbc-dmer-dev -n func-rsbc-dmer-workflow-orchestrator-dev --query "[?name=='MERCURY_API_KEY'].value" -o tsv)"
```

`mercuryApiKey` must be supplied on every deploy (it defaults to empty, and an empty
`MERCURY_API_KEY` fails every run with `ConfigError`).

Then the Postgres role (once per environment): run `services/intake-processor/create-principal.sql`
against `postgres` and `roles.sql` against `dmer` with `function_app_name` set to this app's name
(`SET ROLE rsbc_dmer_admin` first). The app reads and updates `mercury_outbox`, `dmer_document`,
`dmer_decision` and `driver_evaluation`.

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
```

The publisher's database behaviour is tested in `libs/dmer_common/tests/integration/test_outbox_publisher.py`
(against a disposable PostgreSQL, `NORMALIZATION_TEST_DSN`).
