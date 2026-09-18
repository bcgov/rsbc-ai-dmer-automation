"""Integration tests for the health server endpoints (Requirement 1.4).

Starts the real stdlib HTTP server on an ephemeral port and issues live HTTP
requests to ``/healthz`` and ``/readyz``, asserting the readiness callback drives
the ``/readyz`` status code. Written as GIVEN/WHEN/THEN behaviour specs.
"""

from __future__ import annotations

import json
import urllib.request
from urllib.error import HTTPError

import pytest
from di_processor.health import HealthServer


def _get(port: int, path: str) -> tuple[int, dict]:
    """Issue a GET and return (status_code, json_body)."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:  # non-2xx (e.g. 503 / 404)
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_healthz_returns_ok_while_serving():
    """GIVEN a running health server WHEN GET /healthz THEN it returns 200 ok."""
    with HealthServer(0, is_ready=lambda: True) as server:
        status, body = _get(server.port, "/healthz")

    assert status == 200
    assert body == {"status": "ok"}


def test_readyz_returns_200_when_ready():
    """GIVEN readiness is True WHEN GET /readyz THEN it returns 200 ready."""
    with HealthServer(0, is_ready=lambda: True) as server:
        status, body = _get(server.port, "/readyz")

    assert status == 200
    assert body == {"status": "ready"}


def test_readyz_returns_503_when_not_ready():
    """GIVEN readiness is False WHEN GET /readyz THEN it returns 503 not-ready."""
    with HealthServer(0, is_ready=lambda: False) as server:
        status, body = _get(server.port, "/readyz")

    assert status == 503
    assert body == {"status": "not-ready"}


def test_readyz_reflects_dynamic_state():
    """GIVEN readiness flips WHEN GET /readyz THEN the status follows the flag."""
    ready = {"value": False}
    with HealthServer(0, is_ready=lambda: ready["value"]) as server:
        first, _ = _get(server.port, "/readyz")
        ready["value"] = True
        second, _ = _get(server.port, "/readyz")

    assert first == 503
    assert second == 200


def test_unknown_path_returns_404():
    """GIVEN a running server WHEN GET /nope THEN it returns 404 not-found."""
    with HealthServer(0, is_ready=lambda: True) as server:
        status, body = _get(server.port, "/nope")

    assert status == 404
    assert body == {"status": "not-found"}


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
