
# di-processor

Azure Container App for the **extraction** stage of the DMER pipeline. Consumes
`raw-dmer-queue`, runs the custom-model top-level extraction + tiled OCR + LLM
handwritten reconstruction, merges them into a combined extraction, persists the
artifacts, and publishes to `extracted-dmer-queue`. See
`docs/services/di-processor.md` for full responsibilities and dependencies.

## Configuration

All configuration is read from environment variables sourced from App
Configuration / Key Vault references (see `.env.example` for the full list):

- `APP_CONFIGURATION_ENDPOINT`, `SERVICE_BUS_NAMESPACE_FQDN`, `POSTGRES_HOST`,
  `BLOB_ACCOUNT_URL`
- `DOC_INTELLIGENCE_ENDPOINT`, `DI_CUSTOM_MODEL_ID` (Managed Identity)
- `AZURE_OPENAI_*` (external endpoint; API key via Key Vault — the one documented
  Managed-Identity exception)
- Queue/container name overrides, `LLM_PROMPT_VERSION`, `HEALTH_PORT`

## Local run

```bash
cp .env.example .env
pip install -e ../../libs/dmer_common
pip install -r requirements.txt
python -m di_processor.main
```

Health endpoints are served on `HEALTH_PORT` (default `8080`): `/healthz`
(liveness) and `/readyz` (readiness).

## Container image

The image installs the sibling `libs/dmer_common`, so it must be built with the
**repository root** as the build context:

```bash
docker build -f services/di-processor/Dockerfile -t di-processor .
docker run --env-file services/di-processor/.env -p 8080:8080 di-processor
```

The image is multi-stage (`python:3.12-slim`), runs as a non-root user, and
declares a `HEALTHCHECK` against `/readyz`.

## Test

```bash
pytest tests/
```
