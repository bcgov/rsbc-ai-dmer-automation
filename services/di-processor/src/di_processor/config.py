"""Typed runtime settings for di-processor.

Every value is read through :mod:`dmer_common.config`, which is the single site
that touches the environment (App Configuration values and Key Vault references
are surfaced as environment variables in Container Apps). Nothing here is
hardcoded per environment (Requirement 8.3); missing required values fail fast
via :func:`dmer_common.config.require`.

The external Azure OpenAI settings (endpoint + Key Vault API key — the single
documented Managed-Identity exception) are loaded via
:func:`dmer_common.config.openai_settings`, not re-read here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dmer_common import config


@dataclass(frozen=True)
class Settings:
    """di-processor runtime configuration.

    Attributes
    ----------
    app_configuration_endpoint:
        Optional App Configuration endpoint. Not read by any code path today
        (all settings arrive as environment variables) and no App
        Configuration store exists yet, so it is not required.
    service_bus_namespace_fqdn:
        Fully-qualified Service Bus namespace (Managed Identity auth).
    dmer_raw_queue:
        Source queue this service consumes (``dmer-raw``).
    dmer_extracted_queue:
        Destination queue for the combined-extraction message (``dmer-extracted``).
    postgres_host:
        PostgreSQL host (Managed Identity token auth).
    postgres_user:
        PostgreSQL role to log in as — the name the di-processor Managed
        Identity was registered under (``create-principal.sql``), i.e. the
        identity's own name.
    blob_account_url:
        Blob storage account URL (``https://<account>.blob.core.windows.net``).
    doc_intelligence_endpoint:
        Document Intelligence private-endpoint URL (Managed Identity).
    custom_model_id:
        Custom-trained DI model id used for Stage A top-level extraction.
    prompt_version:
        Optional version tag for the LLM reconstruction prompt (recorded in the
        combined extraction metadata).
    health_port:
        Port the readiness/liveness HTTP server binds to.
    postgres_database:
        PostgreSQL database holding the pipeline schema (default ``dmer``, the
        database the Flyway migrations and Ingest use).
    postgres_port:
        PostgreSQL port (default ``5432``).
    postgres_sslmode:
        asyncpg ``ssl`` mode (default ``require``; Azure rejects non-TLS).
    postgres_password:
        Local development only. When set it is used as-is; when unset (every
        deployed environment) a fresh Entra token is fetched per connection.
    """

    app_configuration_endpoint: str | None
    service_bus_namespace_fqdn: str
    dmer_raw_queue: str
    dmer_extracted_queue: str
    postgres_host: str
    postgres_user: str
    blob_account_url: str
    doc_intelligence_endpoint: str
    custom_model_id: str
    prompt_version: str | None
    health_port: int
    postgres_database: str = "dmer"
    postgres_port: int = 5432
    postgres_sslmode: str = "require"
    postgres_password: str | None = field(default=None, repr=False)


def load_settings() -> Settings:
    """Load and validate di-processor settings from configuration.

    Raises :class:`dmer_common.config.ConfigError` if a required value is
    missing, so the service fails fast at startup rather than mid-pipeline.
    """
    port = config.get("HEALTH_PORT", "8080") or "8080"
    return Settings(
        app_configuration_endpoint=config.get("APP_CONFIGURATION_ENDPOINT") or None,
        service_bus_namespace_fqdn=config.require("SERVICE_BUS_NAMESPACE_FQDN"),
        dmer_raw_queue=config.get("DMER_RAW_QUEUE", "dmer-raw") or "dmer-raw",
        dmer_extracted_queue=config.get("DMER_EXTRACTED_QUEUE", "dmer-extracted")
        or "dmer-extracted",
        postgres_host=config.require("POSTGRES_HOST"),
        postgres_user=config.require("POSTGRES_USER"),
        blob_account_url=config.require("BLOB_ACCOUNT_URL"),
        doc_intelligence_endpoint=config.require("DOC_INTELLIGENCE_ENDPOINT"),
        custom_model_id=config.require("DI_CUSTOM_MODEL_ID"),
        prompt_version=config.get("LLM_PROMPT_VERSION"),
        health_port=int(port),
        postgres_database=config.get("POSTGRES_DATABASE", "dmer") or "dmer",
        postgres_port=int(config.get("POSTGRES_PORT", "5432") or "5432"),
        postgres_sslmode=config.get("POSTGRES_SSLMODE", "require") or "require",
        postgres_password=config.get("POSTGRES_PASSWORD") or None,
    )
