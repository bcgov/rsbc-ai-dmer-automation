"""Mercury bearer tokens: GET Authentication/token?secret=, reuse until expiry,
a new one on expiry or on a 401 (faked HTTP, fake clock)."""

from __future__ import annotations

import base64
import json
import logging

import pytest
from dmer_common.config import (
    MercuryAuthSettings,
    MercurySettings,
    MercuryWriteSettings,
)
from dmer_common.mercury_client import MercuryApiError, MercuryClient, MercuryWriteError
from dmer_common.mercury_client import auth as auth_module
from dmer_common.mercury_client.auth import MercuryAuth, MercuryAuthError, shared_auth
from dmer_common.retry import CircuitBreaker

SECRET = "s3cr3t/key+="  # pragma: allowlist secret
AUTH = MercuryAuthSettings(
    token_url="https://mercury.test/Authentication/token",
    api_key=SECRET,
    default_ttl_seconds=600,
)
TOKEN_URL = "https://mercury.test/Authentication/token?secret=s3cr3t%2Fkey%2B%3D"


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TokenServer:
    """Answers the token endpoint with token-1, token-2, ... (or *responses*)."""

    def __init__(self, *responses: tuple[int, bytes]) -> None:
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        self.urls.append(url)
        if self.responses:
            return self.responses.pop(0)
        return 200, json.dumps({"token": f"token-{len(self.urls)}"}).encode()


def _auth(server: TokenServer, clock: Clock | None = None) -> MercuryAuth:
    return MercuryAuth(AUTH, http_get=server, clock=clock or Clock())


def _jwt(exp: float) -> str:
    def part(obj: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{part({'alg': 'none'})}.{part({'exp': exp})}.sig"


# -- fetching ----------------------------------------------------------------


def test_token_is_fetched_with_the_secret_in_the_query():
    server = TokenServer()
    assert _auth(server).token() == "token-1"
    assert server.urls == [TOKEN_URL]


@pytest.mark.parametrize(
    "body",
    [
        b'{"token": "abc"}',
        b'{"accessToken": "abc", "expiresIn": 3600}',
        b'{"access_token": "abc"}',
        b'"abc"',
        b"abc",
        b"Bearer abc",
        b'{"token": "Bearer abc"}',
    ],
)
def test_token_response_forms(body):
    assert _auth(TokenServer((200, body))).token() == "abc"


@pytest.mark.parametrize("body", [b"", b"{}", b'{"token": ""}', b"[]"])
def test_a_response_without_a_token_raises(body):
    with pytest.raises(MercuryAuthError, match="no token"):
        _auth(TokenServer((200, body))).token()


def test_a_failed_token_request_raises_with_the_status():
    with pytest.raises(MercuryAuthError) as raised:
        _auth(TokenServer((401, b"bad secret"))).token()
    assert raised.value.status == 401


def test_neither_the_secret_nor_the_token_is_logged(caplog):
    with caplog.at_level(logging.DEBUG, logger=auth_module._log.name):
        _auth(TokenServer()).token()
        with pytest.raises(MercuryAuthError):
            _auth(TokenServer((500, b"boom"))).token()
    logged = caplog.text + " ".join(str(r.__dict__) for r in caplog.records)
    assert SECRET not in logged
    assert "s3cr3t" not in logged
    assert "token-1" not in logged


# -- reuse and expiry -----------------------------------------------------------


def test_the_token_is_reused_until_it_expires():
    server, clock = TokenServer(), Clock()
    auth = _auth(server, clock)
    assert auth.token() == "token-1"
    clock.now += 500  # default TTL 600 s, refreshed 60 s early
    assert auth.token() == "token-1"
    clock.now += 41
    assert auth.token() == "token-2"
    assert len(server.urls) == 2


def test_expires_in_from_the_response_is_used():
    server = TokenServer((200, b'{"token": "a", "expiresIn": 3600}'))
    clock = Clock()
    auth = _auth(server, clock)
    auth.token()
    clock.now += 3000
    assert auth.token() == "a"  # still valid: 3600 s, refreshed at 3540 s
    clock.now += 600
    assert auth.token() == "token-2"


def test_expires_at_from_the_response_is_used():
    clock = Clock(1_700_000_000.0)  # 2023-11-14T22:13:20Z
    body = b'{"token": "a", "expiresAt": "2023-11-14T23:13:20Z"}'
    auth = _auth(TokenServer((200, body)), clock)
    auth.token()
    clock.now += 3500
    assert auth.token() == "a"
    clock.now += 100
    assert auth.token() == "token-2"


def test_a_jwt_expiry_is_used_when_the_response_gives_none():
    clock = Clock()
    token = _jwt(clock.now + 120)
    auth = _auth(TokenServer((200, json.dumps({"token": token}).encode())), clock)
    assert auth.token() == token
    clock.now += 61  # 120 s token, refreshed after 60 s
    assert auth.token() == "token-2"


def test_invalidate_drops_only_the_rejected_token():
    server = TokenServer()
    auth = _auth(server)
    auth.token()
    auth.invalidate("someone-elses-token")
    assert auth.token() == "token-1"
    auth.invalidate("token-1")
    assert auth.token() == "token-2"


def test_shared_auth_is_one_instance_per_token_url_and_key(monkeypatch):
    monkeypatch.setattr(auth_module, "_shared", {})
    first = shared_auth(AUTH, http_get=TokenServer())
    assert shared_auth(AUTH, http_get=TokenServer()) is first
    other = MercuryAuthSettings(token_url="https://other.test/token", api_key=SECRET)
    assert shared_auth(other, http_get=TokenServer()) is not first


# -- the client: bearer header, and a new token on a 401 ------------------------

BATCH = MercurySettings(base_url="https://mercury.test/Dmers", api_key=SECRET)
PAGE = json.dumps({"items": [], "nextLink": None}).encode()


class Mercury:
    """Token endpoint + GET dmers; GET dmers accepts only *valid* tokens."""

    def __init__(self, valid: set[str]) -> None:
        self.valid = valid
        self.tokens_issued = 0
        self.api_calls: list[str] = []

    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]:
        if url.startswith(AUTH.token_url):
            self.tokens_issued += 1
            return 200, json.dumps({"token": f"token-{self.tokens_issued}"}).encode()
        bearer = headers.get("Authorization", "")
        self.api_calls.append(bearer)
        if bearer.removeprefix("Bearer ") in self.valid:
            return 200, PAGE
        return 401, b""


def _client(mercury: Mercury) -> MercuryClient:
    return MercuryClient(
        settings=BATCH,
        http_get=mercury,
        auth=MercuryAuth(AUTH, http_get=mercury, clock=Clock()),
    )


def test_api_calls_carry_the_fetched_token_not_the_key():
    mercury = Mercury(valid={"token-1"})
    _client(mercury).get_page()
    assert mercury.api_calls == ["Bearer token-1"]


def test_a_401_fetches_a_new_token_and_calls_once_more():
    # token-1 has expired on Mercury's side before our expiry estimate
    mercury = Mercury(valid={"token-2"})
    client = _client(mercury)
    client.get_page()
    assert mercury.api_calls == ["Bearer token-1", "Bearer token-2"]
    client.get_page()  # the new token is kept
    assert mercury.api_calls[-1] == "Bearer token-2"
    assert mercury.tokens_issued == 2


def test_a_second_401_is_an_api_error():
    mercury = Mercury(valid=set())
    with pytest.raises(MercuryApiError):
        _client(mercury).get_page()


def test_the_default_client_reads_the_token_settings(monkeypatch):
    monkeypatch.setattr(auth_module, "_shared", {})
    monkeypatch.setenv(
        "MERCURY_AUTH_TOKEN_URL", "https://mercury.test/Authentication/token/"
    )
    monkeypatch.setenv("MERCURY_API_KEY", SECRET)
    mercury = Mercury(valid={"token-1"})
    MercuryClient(settings=BATCH, http_get=mercury).get_page()
    assert mercury.api_calls == ["Bearer token-1"]


# -- write-back: a token failure is a MercuryWriteError -------------------------

WRITE = MercuryWriteSettings(base_url="https://mercury.test/api", api_key=SECRET)


@pytest.mark.parametrize(("status", "retryable"), [(503, True), (401, False)])
def test_a_token_failure_on_a_write_is_a_write_error(status, retryable):
    def no_token(url, *, headers):
        return status, b""

    client = MercuryClient(
        write_settings=WRITE,
        http_send=lambda *a, **k: (200, b"{}"),
        breaker=CircuitBreaker(),
        auth=MercuryAuth(AUTH, http_get=no_token, clock=Clock()),
    )
    payload = {"document_guid": "g", "outcome_code": "IN", "comment": "c"}
    with pytest.raises(MercuryWriteError) as raised:
        client.send_outbox("UPDATE_OUTCOME", payload, "k")
    assert raised.value.retryable is retryable
