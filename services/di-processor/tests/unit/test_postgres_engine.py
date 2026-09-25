"""Unit tests for the Postgres engine wiring (Managed Identity token auth).

No database is contacted: each test opens a connection through SQLAlchemy's
real connect path, and a recording ``do_connect`` hook (registered after the
engine's own) captures the final connect parameters and aborts before asyncpg
touches the network. Written as GIVEN/WHEN/THEN behaviour specs.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from di_processor.config import Settings
from di_processor.main import AAD_POSTGRES_SCOPE, build_postgres_engine
from sqlalchemy import event

_SETTINGS = Settings(
    app_configuration_endpoint="https://appcfg.example",
    service_bus_namespace_fqdn="sb.example.servicebus.windows.net",
    dmer_raw_queue="dmer-raw",
    dmer_extracted_queue="dmer-extracted",
    postgres_host="pg.example",
    postgres_user="id-rsbc-dmer-di-processor-dev-001",
    blob_account_url="https://acct.blob.core.windows.net",
    doc_intelligence_endpoint="https://di.example",
    custom_model_id="rsbc-ocr-dmer-v9",
    prompt_version=None,
    health_port=0,
)


class _FakeCredential:
    """Returns a new token per call so tests can tell calls apart."""

    def __init__(self) -> None:
        self.scopes: list[str] = []

    def get_token(self, scope: str) -> SimpleNamespace:
        self.scopes.append(scope)
        return SimpleNamespace(token=f"FAKE-token-{len(self.scopes)}")


class _Stop(Exception):
    """Aborts the connect attempt once the parameters have been recorded."""


def _connect_params(engine) -> dict:
    """Open a connection the way the repositories do and return the keyword
    arguments that would have been handed to asyncpg."""
    seen: dict = {}

    @event.listens_for(engine.sync_engine, "do_connect")
    def _record(dialect, conn_rec, cargs, cparams):
        seen.update(cparams)
        raise _Stop

    async def connect() -> None:
        async with engine.connect():
            pass  # pragma: no cover - never reached

    try:
        with pytest.raises(_Stop):
            asyncio.run(connect())
    finally:
        event.remove(engine.sync_engine, "do_connect", _record)
    return seen


def test_logs_in_as_identity_role_to_dmer_over_tls():
    """GIVEN settings WHEN connecting THEN the identity's role, the dmer
    database and TLS are used."""
    params = _connect_params(build_postgres_engine(_SETTINGS, _FakeCredential()))

    assert params["user"] == "id-rsbc-dmer-di-processor-dev-001"
    assert (params["host"], params["port"], params["database"]) == (
        "pg.example",
        5432,
        "dmer",
    )
    assert params["ssl"] == "require"


def test_fresh_entra_token_on_every_connection():
    """GIVEN no password WHEN two connections open THEN each asks the
    credential for a Postgres-scoped token (not one token reused forever)."""
    credential = _FakeCredential()
    engine = build_postgres_engine(_SETTINGS, credential)

    first = _connect_params(engine)
    second = _connect_params(engine)

    assert first["password"] == "FAKE-token-1"
    assert second["password"] == "FAKE-token-2"
    assert credential.scopes == [AAD_POSTGRES_SCOPE, AAD_POSTGRES_SCOPE]


def test_local_password_skips_token():
    """GIVEN POSTGRES_PASSWORD (local dev) WHEN connecting THEN it's used and
    the credential is never asked for a token."""
    credential = _FakeCredential()
    engine = build_postgres_engine(
        replace(_SETTINGS, postgres_password="FAKE-local-pw"), credential
    )

    assert _connect_params(engine)["password"] == "FAKE-local-pw"
    assert credential.scopes == []


def test_sslmode_disable_sends_no_ssl_argument():
    """GIVEN sslmode 'disable' (a local container without TLS) WHEN connecting
    THEN no ssl argument is passed to asyncpg."""
    engine = build_postgres_engine(
        replace(_SETTINGS, postgres_sslmode="disable"), _FakeCredential()
    )

    assert "ssl" not in _connect_params(engine)
