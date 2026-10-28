# di-processor — Azure Container App image.
#
# Multi-stage build on python:3.12-slim, running as a non-root user, with a
# HEALTHCHECK against the readiness endpoint (see docs/development/coding-standards.md
# #azure-container-apps).
#
# IMPORTANT: build with the repository root as the build context so the sibling
# shared library (libs/dmer_common) can be copied in:
#
#   docker build -f services/di-processor/Dockerfile -t di-processor .

# ---- builder: install deps into a venv ------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build into an isolated virtualenv we can copy wholesale into the runtime.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build

# Install the shared library first (its own deps: Azure SDKs, openai, SQLAlchemy,
# asyncpg, tenacity, pydantic), then the service's extra imaging deps.
COPY libs/dmer_common /build/libs/dmer_common
RUN pip install /build/libs/dmer_common

# di-processor extra runtime deps (dmer-common already satisfied above).
RUN pip install "pymupdf>=1.24,<2" "pillow>=10,<12"

# ---- runtime: minimal, non-root -------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HEALTH_PORT=8080

# Copy the prepared virtualenv from the builder.
COPY --from=builder /opt/venv /opt/venv

# Non-root runtime user.
RUN groupadd --system app && useradd --system --gid app --home /app app
WORKDIR /app

# Application source (installed as a package so `di_processor` is importable).
COPY services/di-processor/pyproject.toml /app/pyproject.toml
COPY services/di-processor/src /app/src
RUN pip install --no-deps .

USER app

EXPOSE 8080

# Liveness/readiness: the container is healthy once /readyz reports ready.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; url='http://127.0.0.1:%s/readyz' % os.environ.get('HEALTH_PORT','8080'); sys.exit(0 if urllib.request.urlopen(url, timeout=3).status==200 else 1)"

CMD ["python", "-m", "di_processor.main"]
