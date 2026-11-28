# reliability

The reliability components of the revised architecture (v2.1 §4.6): timer functions that make
sure a decided DMER's outcome actually reaches Mercury. See
`docs/development/stages/09-reliability-components.md`.

It hosts four timer functions: the **Outbox Publisher**, the **DLQ Drain**, the
**Reconciliation Sweeper** and the **Daily Reconciliation** report. Their alerts are
`infrastructure/bicep/monitoring.bicep` (see `docs/operations/monitoring-alerts.md`).

## Outbox Publisher

Post-Processing (inside the Driver Orchestration's decision transaction, and Driver Lookup's
I-12/I-22 route) writes one `mercury_outbox` row per Mercury operation, in the same transaction
as the decision. `OutboxPublisher` drains them about once a minute
(`OUTBOX_PUBLISHER_SCHEDULE`). The logic is in `dmer_common.db.outbox_publisher.publish_due`:

1. claim up to `OUTBOX_BATCH_SIZE` documents with a due `PENDING` row, leasing their rows
   (`OUTBOX_LEASE_SECONDS`) so a crashed run's rows come back rather than being lost;
2. send each document's rows in order -- `MAP_DRIVER`, `CREATE_CASE`, `UPDATE_OUTCOME` -- with the row's idempotency key; a failure stops that document so the
   outcome is never written before its driver or case -- except that a driver link
   (`MAP_DRIVER`) that fails for good still lets the outcome go, as a processing error for
   Intake with the error in its notes (the case write is blocked), and a case write
   (`CREATE_CASE`) that fails for good lets the outcome go as usual, the error in its notes;
3. record `SENT`, or back off exponentially (`OUTBOX_BACKOFF_INITIAL_SECONDS` doubling to
   `OUTBOX_BACKOFF_MAX_SECONDS`), or `FAILED` when Mercury rejects the write (a 4xx) or after
   `OUTBOX_MAX_ATTEMPTS` -- logged at ERROR for the alert; the document's later rows fail as
   "blocked";
4. a document whose rows are all `SENT` goes `POSTING` -> `COMPLETED`; one whose driver link
   failed but whose outcome went (as an error, to Intake) goes `POSTING` -> `MANUAL_REVIEW`, its
   `FAILED` rows kept as the record. Its driver evaluation goes `DECIDED` -> `POSTED` once every
   document of the batch is finished either way. Fallback outcomes for documents already in
   `MANUAL_REVIEW` leave their status alone.

**Mercury's write API:** the outcome goes through Mercury's `SetDmerAIProccessedStatus`
(`dmer_common.mercury_client.ai_status`), the driver mapping through `add_driver`
(`dmer_common.mercury_client.driver_link`) and the case through `add_case`
(`dmer_common.mercury_client.case_link`); Mercury's idempotency behaviour is still open (M-6). `MercuryClient.send_outbox` and the dev
mock (`services/intake-processor/tests/local/mock-mercury-function-app`) implement them; only
those two should need to change as Mercury shares the rest. The mock records every
write under `mock-writes/<source PDF name>/<operation>/` (so a batch prefix such as
`t20261016e-` lists one job's writes) and can simulate an outage with
`MOCK_WRITE_FAIL_STATUS` (e.g. `503`).

Logs carry ids, operations, idempotency keys and HTTP statuses only -- payloads carry clinical
content and licences.

## DLQ Drain

`DlqDrain` reads the dead-letter sub-queue of each pipeline queue (`dmer-ingest`, `dmer-raw`,
`dmer-extracted`, `driver-decision`) every few minutes (`DLQ_DRAIN_SCHEDULE`), up to
`DLQ_DRAIN_BATCH_SIZE` messages per queue. See `docs/development/stages/10-dlq-drain.md`. Per
message, in one transaction (`dmer_common.db.dlq_drain.drain_dead_letter`):

1. **classify** it (`dmer_common.dlq.classify`) from the dead-letter reason and, for a Service Bus
   `MaxDeliveryCountExceeded`, the stage's last recorded error;
2. **a retry could fix it** (`TRANSIENT` / `PROCESSING`: a database or network blip, a 429/5xx,
   invalid model output, a lost lock, delivery attempts used up with no logical error) -> reopen
   the document at the queue's entry status and **re-send** the message (`attempt` + 1, a new
   message id, after `DLQ_REDRIVE_BACKOFF_SECONDS` doubling per re-send), at most
   `DLQ_REDRIVE_MAX_ATTEMPTS` times; after that it goes to a person as below;
3. **can never succeed** (`PERMANENT_BUSINESS`: an unreadable PDF) -> `MANUAL_REVIEW` and the
   fallback outcome queued to `mercury_outbox`: IN, "Unable to AI process due to error" and the reason
   (question I-1) -- the one case that produces a decision;
4. **a logical error or an unknown reason** (`UNKNOWN`) -> `MANUAL_REVIEW`, no decision.

Every message gets a `processing_error` row (category, reason code, `dlq_message_id`, what was
done in `resolution`), unique per message, so a run that dies half-way is safely repeated. A
document that already has a decision or a queued Mercury write is never reopened or given a
second outcome; a dead letter for a document that has since moved on is recorded as `STALE`.
`driver-decision` dead letters are driver-level: re-sent to the driver's session when retriable,
otherwise recorded without touching the documents. `FALLBACK_ENABLED_CATEGORIES` accepts only
`PERMANENT_BUSINESS`.

## Reconciliation Sweeper

`ReconciliationSweeper` runs every ~15 minutes (`SWEEPER_SCHEDULE`); the queries are in
`dmer_common.db.sweeper`. Each sweep commits its records, then publishes:

1. **stalled documents** -- in `RECEIVED` .. `NORMALIZED` with `updated_at` older than the stage's SLA
   (`STAGE_SLA_INGEST/EXTRACT/NORMALIZE_MINUTES`): re-published to the queue the status waits on
   (`dmer-ingest`, `dmer-raw` with the raw PDF, `dmer-extracted` with the combined extraction --
   falling back a stage when an artifact is missing), with a new message id
   `<document_guid>:sweep<n>`; after `SWEEPER_MAX_REPUBLISH` re-publishes for the stage,
   `MANUAL_REVIEW` (no decision: a stall is not a business failure);
2. **stuck driver evaluations** -- open and `WAITING` (or `STALE`) with no document still in flight
   and nothing moving for `STAGE_SLA_DRIVER_MINUTES` (the lost-update race): `WAITING -> STALE` and a
   `driver-decision` signal to the driver's session; the orchestrator takes `STALE -> READY`;
3. **outbox backlog** -- `PENDING` rows past `OUTBOX_BACKLOG_SLA_MINUTES`, rows with
   `OUTBOX_ATTEMPT_ALERT`+ attempts, and `FAILED` rows: logged at ERROR for the alert.

Every re-publish and give-up is a `processing_error` row (`TRANSIENT_STALLED` / `UNKNOWN_STALLED`).
A failed send is retried by the next sweep after the SLA. The fourth documented query (documents in
Mercury's GET-by-licence response with no `dmer_document` row) is not built yet.

## Daily Reconciliation

`DailyReconciliation` runs once a day (`RECONCILIATION_SCHEDULE`, default 13:00 UTC). It reads
the documents still in Mercury's DPS queues (one `GET dmers` page from
`MERCURY_API_BASE_URL`; the API is claim-based) and checks every document against the pipeline
(`dmer_common.db.reconciliation`): never received (after `RECONCILIATION_RECEIVE_GRACE_HOURS`),
overdue in the pipeline (after `RECONCILIATION_OVERDUE_HOURS`), in manual review with nothing sent
to Mercury, not posted, or a `FAILED` write. The report goes to `RECONCILIATION_CONTAINER`
(`reconciliation-reports/<date>.json` on the runtime storage account) with the day's manual-review,
fallback and stage-p95 figures, and its summary is logged for the alerts. What each finding means:
`docs/operations/runbook.md`.

A `FAILED` outbox row is never retried: the publisher logs it once (the `outbox-write-failed`
alert) and a person resolves it in Mercury; the sweeper's backlog alert doesn't count `FAILED` rows.

## Deploy

Infrastructure is `infrastructure/bicep/reliability.bicep` with
`deployment/<env>/reliability.parameters.json`, deployed into the resource group holding the
pipeline's data resources (`rg-rsbc-dmer-dev` in DEV). Prerequisites it references but doesn't
create:

- the shared orchestrator subnet (`snet-rsbc-dmer-orchestrator-<env>`);
- the deployment package container on the runtime storage account
  (`app-package-func-rsbc-dmer-reliability-dev` in DEV).

All four timers deploy **disabled** (`outboxPublisherEnabled` / `dlqDrainEnabled` /
`sweeperEnabled` / `reconciliationEnabled = false`): outbox rows wait as `PENDING`, dead letters
stay in their DLQs and nothing is swept or reported until they are switched on. The template
grants the app's identity receive and send on the four pipeline queues, and Storage Blob Data
Reader on the `extracted-dmer` container of the pipeline data storage account
(`dataStorageAccountName`, `BLOB_ACCOUNT_URL`): when `add_driver` fails for good, the publisher
reads the form's driver details from `combined.json` for the outcome's note.

```bash
az deployment group create -g rg-rsbc-dmer-dev   -f infrastructure/bicep/reliability.bicep   -p @deployment/dev/reliability.parameters.json   -p mercuryApiKey="$(az functionapp config appsettings list -g rg-rsbc-dmer-dev -n func-rsbc-dmer-workflow-orchestrator-dev --query "[?name=='MERCURY_API_KEY'].value" -o tsv)"
```

`mercuryApiKey` must be supplied on every deploy (it defaults to empty, and an empty
`MERCURY_API_KEY` (or `MERCURY_AUTH_TOKEN_URL`) fails every run with `ConfigError`).

Then the Postgres role (once per environment): run `services/intake-processor/create-principal.sql`
against `postgres` and `roles.sql` against `dmer` with `function_app_name` set to this app's name
(`SET ROLE rsbc_dmer_admin` first). The app reads and updates `mercury_outbox`, `dmer_document`,
`dmer_decision`, `driver_evaluation`, `processing_error` and `dmer_stage_run`.

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

The database behaviour is tested in `libs/dmer_common/tests/integration/test_outbox_publisher.py`
, `test_dlq_drain.py` and `test_sweeper.py` (against a disposable PostgreSQL, `NORMALIZATION_TEST_DSN`); the
classification in `libs/dmer_common/tests/test_dlq_classify.py`.
