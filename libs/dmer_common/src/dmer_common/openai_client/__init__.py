"""External Azure OpenAI client (public endpoint, Key Vault API key).

di-processor and normalizer-service share the external Azure OpenAI endpoint —
the single documented exception to "Managed Identity everywhere, zero secrets".
The endpoint and API key are read once, via :mod:`dmer_common.config` (App
Configuration + Key Vault reference); the key is never logged. Calls are wrapped
by the shared retry + circuit-breaker policies (Requirements 6.1, 6.6, 8.3).
"""

from __future__ import annotations

from .client import OpenAIClient

__all__ = ["OpenAIClient"]
