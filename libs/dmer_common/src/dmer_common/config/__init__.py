"""Configuration loader — the only place environment configuration is read.

At runtime, App Configuration values and Key Vault references are surfaced as
environment variables (Container Apps secrets / Function App settings resolve
Key Vault references into env vars). This module centralizes reading them so no
service touches ``os.environ`` for config directly, and so the external Azure
OpenAI endpoint URL + API key (the single documented Managed-Identity exception)
have exactly one read site.

``config.get`` / ``config.require`` cover generic access; :func:`openai_settings`
returns the typed settings the OpenAI client needs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    """Raised when a required configuration value is missing."""


def get(key: str, default: str | None = None) -> str | None:
    """Return a configuration value, or ``default`` if unset."""
    return os.environ.get(key, default)


def require(key: str) -> str:
    """Return a required configuration value, raising if it is missing/empty."""
    value = os.environ.get(key)
    if value is None or value == "":
        raise ConfigError(f"Missing required configuration: {key}")
    return value


@dataclass(frozen=True)
class OpenAISettings:
    """External Azure OpenAI connection settings.

    ``api_key`` originates from a Key Vault reference; it must never be logged.
    """

    endpoint: str
    api_key: str
    deployment: str
    api_version: str


# Environment variable names (populated from App Configuration / Key Vault refs).
OPENAI_ENDPOINT_KEY = "AZURE_OPENAI_ENDPOINT"
OPENAI_API_KEY_KEY = "AZURE_OPENAI_API_KEY"
OPENAI_DEPLOYMENT_KEY = "AZURE_OPENAI_DEPLOYMENT"
OPENAI_API_VERSION_KEY = "AZURE_OPENAI_API_VERSION"


def openai_settings() -> OpenAISettings:
    """Load the external Azure OpenAI settings from configuration.

    The endpoint, deployment, and API version come from App Configuration; the
    API key comes from a Key Vault reference. This is the single read site for
    the documented OpenAI exception to "Managed Identity everywhere".
    """
    return OpenAISettings(
        endpoint=require(OPENAI_ENDPOINT_KEY),
        api_key=require(OPENAI_API_KEY_KEY),
        deployment=require(OPENAI_DEPLOYMENT_KEY),
        api_version=get(OPENAI_API_VERSION_KEY, "2024-10-21") or "2024-10-21",
    )
