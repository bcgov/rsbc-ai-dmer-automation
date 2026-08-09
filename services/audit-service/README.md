
# audit-service

See `docs/services/audit-service.md` for full responsibilities and dependencies.

## Local run

```bash
cp .env.example .env
pip install -e ../../libs/dmer_common
pip install -r requirements.txt
python -m audit_service.main
```

## Test

```bash
pytest tests/
```
