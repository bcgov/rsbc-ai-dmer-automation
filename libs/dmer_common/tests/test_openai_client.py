"""Unit tests for the external Azure OpenAI client wrapper (mocked SDK).

Behaviour specs (GIVEN/WHEN/THEN) for completion, retry on transient failure,
and — critically — that the Key Vault API key is never written to a log line.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from dmer_common.config import OpenAISettings
from dmer_common.openai_client import OpenAIClient
from dmer_common.openai_client import client as client_module

SECRET_KEY = "super-secret-key-value"

SETTINGS = OpenAISettings(
    endpoint="https://external.openai.example.com",
    api_key=SECRET_KEY,
    deployment="gpt-5.1",
    api_version="2024-10-21",
)


class _Message:
    def __init__(self, content: str) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str) -> None:
        self.message = _Message(content)


class _Response:
    def __init__(self, content: str) -> None:
        self.choices = [_Choice(content)]


class _FakeCompletions:
    def __init__(self, outer) -> None:
        self._outer = outer

    def create(self, *, model, messages, **kwargs):
        self._outer.calls.append({"model": model, "messages": messages, **kwargs})
        if self._outer.fail_times > 0:
            self._outer.fail_times -= 1
            raise RuntimeError("transient openai error")
        return _Response("reconstructed json")


class _FakeChat:
    def __init__(self, outer) -> None:
        self.completions = _FakeCompletions(outer)


class _FakeSdkClient:
    def __init__(self, *, fail_times: int = 0) -> None:
        self.fail_times = fail_times
        self.calls: list[dict] = []
        self.chat = _FakeChat(self)


def test_complete_returns_message_content():
    # GIVEN a client with a mocked SDK
    sdk = _FakeSdkClient()
    client = OpenAIClient(settings=SETTINGS, sdk_client=sdk)
    # WHEN requesting a completion
    out = client.complete([{"role": "user", "content": "hi"}])
    # THEN the first choice's content is returned and the deployment used
    assert out == "reconstructed json"
    assert sdk.calls[0]["model"] == "gpt-5.1"


def test_complete_retries_transient_failure():
    # GIVEN a client that fails once then succeeds
    sdk = _FakeSdkClient(fail_times=1)
    client = OpenAIClient(settings=SETTINGS, sdk_client=sdk)
    # WHEN requesting a completion THEN the retry policy recovers
    assert client.complete([{"role": "user", "content": "hi"}]) == "reconstructed json"
    assert len(sdk.calls) == 2


def test_api_key_is_never_logged(monkeypatch):
    # GIVEN the module logger writing to an in-memory buffer
    buffer = io.StringIO()
    handler = client_module._log.handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    handler.setStream(buffer)

    sdk = _FakeSdkClient()
    client = OpenAIClient(settings=SETTINGS, sdk_client=sdk)
    # WHEN a completion is logged
    client.complete([{"role": "user", "content": "secret prompt"}])
    # THEN the emitted JSON contains neither the API key nor the message content
    logged = buffer.getvalue()
    assert SECRET_KEY not in logged
    record = json.loads(logged.strip().splitlines()[-1])
    assert record["model"] == "gpt-5.1"
    assert record["message_count"] == 1
    assert "secret prompt" not in logged


def test_settings_default_from_config(monkeypatch):
    # GIVEN OpenAI config in the environment and no explicit settings
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://e.example.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_DEPLOYMENT", "gpt-x")
    sdk = _FakeSdkClient()
    # WHEN constructing without settings THEN it loads from config
    client = OpenAIClient(sdk_client=sdk)
    assert client.complete([{"role": "user", "content": "hi"}]) == "reconstructed json"
    assert sdk.calls[0]["model"] == "gpt-x"


def test_config_require_raises_when_missing(monkeypatch):
    # GIVEN a missing required OpenAI setting
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    from dmer_common.config import ConfigError, openai_settings

    # WHEN loading settings THEN it raises a ConfigError
    with pytest.raises(ConfigError):
        openai_settings()
