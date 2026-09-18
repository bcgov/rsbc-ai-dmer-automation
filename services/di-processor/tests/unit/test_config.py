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
    "APP_CONFIGURATION_ENDPOINT": "https://appcfg.example",
    "SERVICE_BUS_NAMESPACE_FQDN": "sb.example.servicebus.windows.net",
    "POSTGRES_HOST": "pg.example",
    "BLOB_ACCOUNT_URL": "https://acct.blob.core.windows.net",
    "DOC_INTELLIGENCE_ENDPOINT": "https://di.example",
    "DI_CUSTOM_MODEL_ID": "rsbc-ocr-dmer-v9",
}


def _set_required(monkeypatch):
    for key, value in _REQUIRED.items():
        monkeypatch.setenv(key, value)
    # Ensure optional/defaulted vars are unset for a clean baseline.
    for key in (
        "RAW_DMER_QUEUE",
        "EXTRACTED_DMER_QUEUE",
        "LLM_PROMPT_VERSION",
        "HEALTH_PORT",
    ):
        monkeypatch.delenv(key, raising=False)


def test_loads_required_and_applies_defaults(monkeypatch):
    """GIVEN all required vars WHEN load_settings THEN defaults fill the rest."""
    _set_required(monkeypatch)

    settings = load_settings()

    assert settings.custom_model_id == "rsbc-ocr-dmer-v9"
    assert settings.raw_dmer_queue == "raw-dmer-queue"
    assert settings.extracted_dmer_queue == "extracted-dmer-queue"
    assert settings.prompt_version is None
    assert settings.health_port == 8080


def test_overrides_defaults_from_env(monkeypatch):
    """GIVEN overrides set WHEN load_settings THEN the env values win."""
    _set_required(monkeypatch)
    monkeypatch.setenv("RAW_DMER_QUEUE", "raw-dmer-queue-alt")
    monkeypatch.setenv("EXTRACTED_DMER_QUEUE", "extracted-dmer-queue-alt")
    monkeypatch.setenv("LLM_PROMPT_VERSION", "v3")
    monkeypatch.setenv("HEALTH_PORT", "9090")

    settings = load_settings()

    assert settings.raw_dmer_queue == "raw-dmer-queue-alt"
    assert settings.extracted_dmer_queue == "extracted-dmer-queue-alt"
    assert settings.prompt_version == "v3"
    assert settings.health_port == 9090


def test_missing_required_raises_config_error(monkeypatch):
    """GIVEN a required var missing WHEN load_settings THEN ConfigError raised."""
    _set_required(monkeypatch)
    monkeypatch.delenv("BLOB_ACCOUNT_URL", raising=False)

    with pytest.raises(ConfigError):
        load_settings()
