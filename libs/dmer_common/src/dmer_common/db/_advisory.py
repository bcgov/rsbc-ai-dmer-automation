"""A dedicated DB connection holding a PostgreSQL advisory lock.

Used by the per-document activity units of work (Normalize, Rule Engine) so a
retried or replayed activity cannot interleave with -- or partially apply on
top of -- another attempt for the same document.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ..telemetry import get_logger

_log = get_logger(__name__)


@asynccontextmanager
async def advisory_lock_session(
    engine: AsyncEngine, lock_key: str, *, busy_error: Callable[[], Exception]
) -> AsyncIterator[AsyncConnection]:
    """Yield one connection that holds the advisory lock for *lock_key*.

    The lock is taken with ``pg_try_advisory_lock`` -- contention fails fast
    with ``busy_error()`` -- and held for the whole context. On exit the lock
    is released and the connection closed; a cleanup failure never replaces
    the error that ended the context.
    """
    conn = await engine.connect()
    lock_acquired = False

    try:
        lock_result = await conn.execute(
            text("SELECT pg_try_advisory_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": lock_key},
        )
        lock_acquired = bool(lock_result.scalar_one())
        await conn.commit()

        if not lock_acquired:
            raise busy_error()

        yield conn
    finally:
        active_exc = sys.exc_info()[1]
        cleanup_exc: Exception | None = None
        try:
            if lock_acquired:
                try:
                    await conn.execute(
                        text(
                            "SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"
                        ),
                        {"lock_key": lock_key},
                    )
                    await conn.commit()
                except Exception as exc:  # noqa: BLE001
                    cleanup_exc = exc
                    try:
                        await conn.invalidate()
                    # Still close the session and keep the original error.
                    except Exception:  # noqa: BLE001
                        _log.error(
                            "database session invalidation failed",
                            extra={"lock": lock_key.split(":", 1)[0]},
                        )
        finally:
            try:
                await conn.close()
            except Exception as exc:  # noqa: BLE001 - preserve original operation error
                if cleanup_exc is None:
                    cleanup_exc = exc

        if cleanup_exc is not None and active_exc is None:
            raise cleanup_exc
