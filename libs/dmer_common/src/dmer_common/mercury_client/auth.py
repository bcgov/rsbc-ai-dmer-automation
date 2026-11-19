"""Bearer tokens for the Mercury APIs.

Every Mercury API takes ``Authorization: Bearer <token>``. The token comes
from ``GET {MERCURY_AUTH_TOKEN_URL}?secret={MERCURY_API_KEY}``; it is reused
until it expires and then fetched again. A 401 from any API also means the
token is no longer good: the caller drops it (:meth:`MercuryAuth.invalidate`)
and fetches a new one.

One token is shared by every client in the process (:func:`shared_auth`), and
fetched under a lock, so concurrent calls don't each ask for their own.

The secret is part of the token URL, so neither the URL nor the response
(the token) is ever logged.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol
from urllib.parse import quote

from ..config import MercuryAuthSettings
from ..telemetry import get_logger

_log = get_logger(__name__)

# A token is refreshed this long before its expiry, so a call never starts
# with a token about to lapse.
REFRESH_MARGIN_SECONDS = 60

_TOKEN_KEYS = ("token", "accessToken", "access_token", "bearerToken", "bearer_token")
_EXPIRES_IN_KEYS = ("expiresIn", "expires_in")
_EXPIRES_AT_KEYS = ("expiresAt", "expires_at", "expiration", "expires", "expiryDate")


class MercuryAuthError(RuntimeError):
    """The token request failed (``status`` is Mercury's HTTP status, or None
    when the response held no token)."""

    def __init__(self, message: str, *, status: int | None) -> None:
        super().__init__(message)
        self.status = status


class _HttpGet(Protocol):
    def __call__(self, url: str, *, headers: dict[str, str]) -> tuple[int, bytes]: ...


def _jwt_expiry(token: str) -> float | None:
    """The ``exp`` claim of a JWT (epoch seconds), or None if *token* is not
    one. The token is not verified -- only its lifetime is read."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload)).get("exp")
    except (ValueError, AttributeError):
        return None
    return float(exp) if isinstance(exp, int | float) else None


def _parse(body: bytes) -> tuple[str, dict[str, Any]]:
    """The token, and the JSON object it came in (empty when Mercury sent the
    token as plain text or a JSON string)."""
    text = body.decode("utf-8").strip()
    try:
        payload: Any = json.loads(text)
    except ValueError:
        payload = text
    fields: dict[str, Any] = {}
    if isinstance(payload, dict):
        fields = payload
        payload = next((payload[k] for k in _TOKEN_KEYS if payload.get(k)), None)
    if not isinstance(payload, str) or not payload.strip():
        raise MercuryAuthError("Mercury token response held no token", status=None)
    token = payload.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    return token, fields


def _expiry(token: str, fields: dict[str, Any], now: float, default_ttl: int) -> float:
    """When the token expires (epoch seconds): Mercury's ``expiresIn`` or
    ``expiresAt`` if given, else the JWT's ``exp``, else *default_ttl*."""
    for key in _EXPIRES_IN_KEYS:
        value = fields.get(key)
        if isinstance(value, int | float) or (
            isinstance(value, str) and value.isdigit()
        ):
            return now + float(value)
    for key in _EXPIRES_AT_KEYS:
        value = fields.get(key)
        if isinstance(value, str) and value:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
    return _jwt_expiry(token) or now + default_ttl


class MercuryAuth:
    """Fetches, caches and refreshes the Mercury bearer token."""

    def __init__(
        self,
        settings: MercuryAuthSettings,
        *,
        http_get: _HttpGet,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._http_get = http_get
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._refresh_at = 0.0

    def token(self) -> str:
        """A token that is not about to expire -- the cached one, or a new one."""
        with self._lock:
            if self._token is None or self._clock() >= self._refresh_at:
                self._token, self._refresh_at = self._fetch()
            return self._token

    def invalidate(self, token: str) -> None:
        """Drop *token* (Mercury rejected it), unless another call has already
        replaced it with a new one."""
        with self._lock:
            if self._token == token:
                self._token = None

    def _fetch(self) -> tuple[str, float]:
        url = f"{self._settings.token_url}?secret={quote(self._settings.api_key, safe='')}"
        status, body = self._http_get(url, headers={"Accept": "application/json"})
        if status < 200 or status >= 300:
            _log.error("mercury token request failed", extra={"status": status})
            raise MercuryAuthError(
                f"Mercury token request returned HTTP {status}", status=status
            )
        now = self._clock()
        token, fields = _parse(body)
        expires_at = _expiry(token, fields, now, self._settings.default_ttl_seconds)
        lifetime = max(expires_at - now, 0.0)
        # Refresh a minute early -- or halfway through, for a short-lived token.
        refresh_at = now + max(lifetime - REFRESH_MARGIN_SECONDS, lifetime / 2)
        _log.info("mercury token fetched", extra={"expires_in_seconds": int(lifetime)})
        return token, refresh_at


_shared: dict[tuple[str, str], MercuryAuth] = {}
_shared_lock = threading.Lock()


def shared_auth(settings: MercuryAuthSettings, *, http_get: _HttpGet) -> MercuryAuth:
    """The process-wide :class:`MercuryAuth` for *settings*, so every client
    (and every concurrent call) reuses one token."""
    key = (settings.token_url, settings.api_key)
    with _shared_lock:
        if key not in _shared:
            _shared[key] = MercuryAuth(settings, http_get=http_get)
        return _shared[key]
