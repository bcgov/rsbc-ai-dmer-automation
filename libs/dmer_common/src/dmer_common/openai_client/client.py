"""External Azure OpenAI client wrapper.

Exposes ``complete(...)`` (chat/vision completion) against the external endpoint.
The endpoint/deployment/api-version and Key Vault API key come from
:func:`dmer_common.config.openai_settings`; the key is held only on the SDK
client and is never written to a log line. Calls are retried with backoff and
guarded by a circuit breaker.

The underlying SDK client is injectable so unit tests supply a mock and can
assert no key is logged.
"""

from __future__ import annotations

from typing import Any

from ..config import OpenAISettings, openai_settings
from ..retry import CircuitBreaker, openai_breaker, openai_retry
from ..telemetry import get_logger

_log = get_logger(__name__)


class OpenAIClient:
    """External Azure OpenAI wrapper with retry + circuit breaker.

    Parameters
    ----------
    settings:
        Optional connection settings; defaults to
        :func:`dmer_common.config.openai_settings`.
    sdk_client:
        Optional pre-built ``AzureOpenAI`` client (used by tests to inject a
        mock). When provided, ``settings`` is only used for the default
        deployment name.
    breaker:
        Optional shared circuit breaker (defaults to an OpenAI-tuned breaker).
    """

    def __init__(
        self,
        *,
        settings: OpenAISettings | None = None,
        sdk_client: Any | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._settings = settings or openai_settings()
        self._breaker = breaker or openai_breaker()
        if sdk_client is not None:
            self._client = sdk_client
        else:
            from openai import AzureOpenAI

            # api_key comes from Key Vault via config; never logged.
            self._client = AzureOpenAI(
                azure_endpoint=self._settings.endpoint,
                api_key=self._settings.api_key,
                api_version=self._settings.api_version,
            )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Run a chat/vision completion and return the first message's content.

        Retried with backoff and guarded by the circuit breaker. Only the model
        name and message count are logged — never message content or the API key.
        """
        deployment = model or self._settings.deployment

        @openai_retry()
        def _run() -> str:
            response = self._client.chat.completions.create(
                model=deployment, messages=messages, **kwargs
            )
            return response.choices[0].message.content or ""

        _log.info(
            "requesting completion",
            extra={"model": deployment, "message_count": len(messages)},
        )
        return self._breaker.call(_run)
