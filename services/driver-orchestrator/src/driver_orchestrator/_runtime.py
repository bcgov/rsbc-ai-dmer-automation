"""Runtime helpers for this Functions app.

Same as workflow-orchestrator's ``activities/_runtime.py`` (PR #22): a
blocking call runs off the shared event loop, and each invocation builds its
own Postgres engine with a Managed Identity token. Candidates to move into
dmer_common once both apps settle.
"""

from __future__ import annotations

import asyncio
import functools
import os
from typing import Any

from azure.identity import DefaultAzureCredential
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_AAD_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


async def run_sync(fn, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking call in a thread so it doesn't stall the event loop
    other concurrent invocations of this Functions host share."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(fn, *args, **kwargs))


async def get_async_engine() -> AsyncEngine:
    """A fresh async Postgres engine for this invocation (NullPool: no pooling
    across invocations). Uses POSTGRES_PASSWORD if set, else a Managed
    Identity token."""
    host = os.environ["POSTGRES_HOST"]
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    database = os.environ.get("POSTGRES_DATABASE", "dmer")
    user = os.environ["POSTGRES_USER"]
    password = os.environ.get("POSTGRES_PASSWORD")
    sslmode = os.environ.get("POSTGRES_SSLMODE", "require")

    if not password:
        credential = DefaultAzureCredential()
        try:
            token = await run_sync(credential.get_token, _AAD_POSTGRES_SCOPE)
            password = token.token
        finally:
            await run_sync(credential.close)

    url = URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password,
        host=host,
        port=port,
        database=database,
    )
    connect_args = {} if sslmode == "disable" else {"ssl": sslmode}
    return create_async_engine(url, poolclass=NullPool, connect_args=connect_args)
