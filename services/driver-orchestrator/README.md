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
   document, in one transaction.

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

Not built yet: Bicep and CI wiring; the hand-off to Post-Processing.

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
