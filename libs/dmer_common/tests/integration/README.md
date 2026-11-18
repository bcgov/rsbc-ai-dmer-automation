# dmer_common integration tests

Integration tests exercise the shared clients against emulated/dev infrastructure
(Azurite, the Service Bus emulator or a dev namespace, PostgreSQL). They are
**skipped** automatically when the required connection settings or drivers are
absent, so the default `pytest` run (unit only) stays green locally and in CI
lanes without infrastructure.

Enable them by providing the relevant environment variables:

| Test | Requires | Env var(s) |
|---|---|---|
| `test_messaging_integration.py` | Service Bus emulator / dev namespace | `SERVICE_BUS_CONNECTION_STRING`, `SERVICE_BUS_TEST_QUEUE` |
| `test_db_integration.py` | PostgreSQL + async driver (`asyncpg`) | `POSTGRES_TEST_DSN` (e.g. `postgresql+asyncpg://...`) |

Run only integration tests:

```bash
pytest tests/integration
```
