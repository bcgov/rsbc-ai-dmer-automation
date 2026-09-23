"""Liveness/readiness HTTP server for the Container App probes (Requirement 1.4).

Exposes two endpoints on a lightweight stdlib HTTP server (no extra web
dependency), served on a background thread so it never blocks the consumer loop:

- ``GET /healthz`` — liveness: the process is up. Always ``200`` while serving.
- ``GET /readyz``  — readiness: config is loaded and dependencies are reachable.
  Delegates to an injected callback; ``200`` when ready, ``503`` otherwise.

The readiness callback is supplied by ``main.py`` so this module owns no
business logic and stays trivially testable.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Self

from dmer_common.telemetry import get_logger

_log = get_logger(__name__)

ReadinessCheck = Callable[[], bool]


def _make_handler(is_ready: ReadinessCheck) -> type[BaseHTTPRequestHandler]:
    """Build a request handler bound to the given readiness check."""

    class _HealthHandler(BaseHTTPRequestHandler):
        def _respond(self, status: int, body: dict[str, Any]) -> None:
            payload = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self._respond(200, {"status": "ok"})
            elif self.path == "/readyz":
                if is_ready():
                    self._respond(200, {"status": "ready"})
                else:
                    self._respond(503, {"status": "not-ready"})
            else:
                self._respond(404, {"status": "not-found"})

        def log_message(self, *args: Any) -> None:  # silence stdlib access logging
            return

    return _HealthHandler


class HealthServer:
    """A background health server exposing ``/healthz`` and ``/readyz``."""

    def __init__(self, port: int, is_ready: ReadinessCheck, *, host: str = "") -> None:
        self._server = ThreadingHTTPServer((host, port), _make_handler(is_ready))
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="di-processor-health",
            daemon=True,
        )

    @property
    def port(self) -> int:
        """The port the server is bound to (useful when ``0`` was requested)."""
        return self._server.server_address[1]

    def start(self) -> None:
        """Start serving on a daemon thread."""
        self._thread.start()
        _log.info("health server started", extra={"port": self.port})

    def stop(self) -> None:
        """Stop serving and release the socket."""
        self._server.shutdown()
        self._server.server_close()
        _log.info("health server stopped")

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
