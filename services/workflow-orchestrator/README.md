
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
