
# di-processor

See `docs/services/di-processor.md` for full responsibilities and dependencies.

## Local run

```bash
cp .env.example .env
pip install -e ../../libs/dmer_common
pip install -r requirements.txt
python -m di_processor.main
```

## Test

```bash
pytest tests/
```
