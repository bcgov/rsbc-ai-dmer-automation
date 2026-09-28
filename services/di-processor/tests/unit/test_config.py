"""Unit tests for typed settings loading (Requirement 8.3).

Config is read only through ``dmer_common.config`` (env-backed); these tests set
environment variables and assert required-value validation and defaults. Written
as GIVEN/WHEN/THEN behaviour specs.
"""

from __future__ import annotations

import pytest
from di_processor.config import load_settings
from dmer_common.config import ConfigError

_REQUIRED = {
    "SERVICE_BUS_NAMESPACE_FQDN": "sb.example.servicebus.windows.net",
    "POSTGRES_HOST": "pg.example",
    "POSTGRES_USER": "id-rsbc-dmer-di-processor-dev-001",
    "BLOB_ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "DOC_INTELLIGENCE_ENDPOINT": "https://di.example",
    "DI_CUSTOM_MODEL_ID": "rsbc-ocr-dmer-v9",
}


def _set_required(monkeypatch):
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    # Ensure optional/defaulted vars are unset for a clean baseline.
    for key in (
        "APP_CONFIGURATION_ENDPOINT",
        "DMER_RAW_QUEUE",
        "DMER_EXTRACTED_QUEUE",
        "LLM_PROMPT_VERSION",
        "HEALTH_PORT",
        "POSTGRES_DATABASE",
        "POSTGRES_PORT",
        "POSTGRES_SSLMODE",
        "POSTGRES_PASSWORD",
    ):
        monkeypatch.delenv(key, raising=False)


def test_loads_required_and_applies_defaults(monkeypatch):
    """GIVEN all required vars WHEN load_settings THEN defaults fill the rest."""
    _set_required(monkeypatch)

    settings = load_settings()

    assert settings.custom_model_id == "rsbc-ocr-dmer-v9"
    assert settings.dmer_raw_queue == "dmer-raw"
    assert settings.dmer_extracted_queue == "dmer-extracted"
    assert settings.prompt_version is None
    assert settings.health_port == 8080
    # the pipeline schema lives in the "dmer" database (Flyway + Ingest)
    assert settings.postgres_database == "dmer"
    # deployed default: Entra token auth over TLS, no password
    assert settings.postgres_user == "id-rsbc-dmer-di-processor-dev-001"
    assert settings.postgres_port == 5432
    assert settings.postgres_sslmode == "require"
    assert settings.postgres_password is None
    # unused and no store exists yet: optional, not a startup failure
    assert settings.app_configuration_endpoint is None


def test_overrides_defaults_from_env(monkeypatch):
    """GIVEN overrides set WHEN load_settings THEN the env values win."""
    _set_required(monkeypatch)
    monkeypatch.setenv("DMER_RAW_QUEUE", "dmer-raw-alt")
    monkeypatch.setenv("DMER_EXTRACTED_QUEUE", "dmer-extracted-alt")
    monkeypatch.setenv("LLM_PROMPT_VERSION", "v3")
    monkeypatch.setenv("HEALTH_PORT", "9090")
    monkeypatch.setenv("POSTGRES_DATABASE", "dmer_test")

    settings = load_settings()

    assert settings.dmer_raw_queue == "dmer-raw-alt"
    assert settings.postgres_database == "dmer_test"
    assert settings.dmer_extracted_queue == "dmer-extracted-alt"
    assert settings.prompt_version == "v3"
    assert settings.health_port == 9090


def test_missing_required_raises_config_error(monkeypatch):
    """GIVEN a required var missing WHEN load_settings THEN ConfigError raised."""
    _set_required(monkeypatch)
    monkeypatch.delenv("BLOB_ACCOUNT_URL", raising=False)

    with pytest.raises(ConfigError):
        load_settings()


def test_missing_postgres_user_raises_config_error(monkeypatch):
    """GIVEN no POSTGRES_USER WHEN load_settings THEN ConfigError (no silent
    fallback to a role the Managed Identity isn't registered as)."""
    _set_required(monkeypatch)
    monkeypatch.delenv("POSTGRES_USER", raising=False)

    with pytest.raises(ConfigError):
        load_settings()


def test_postgres_password_is_not_in_repr(monkeypatch):
    """GIVEN a local-dev POSTGRES_PASSWORD WHEN settings are printed THEN the
    password is not."""
    _set_required(monkeypatch)
    monkeypatch.setenv("POSTGRES_PASSWORD", "FAKE-local-pw")

    settings = load_settings()

    assert settings.postgres_password == "FAKE-local-pw"  # pragma: allowlist secret
    assert "FAKE-local-pw" not in repr(settings)
