"""Integration test: PostgresIdempotencyStore against PostgreSQL.

Skipped unless ``POSTGRES_TEST_DSN`` is set and ``asyncpg`` is importable. Proves
the durable, atomic claim semantics across **separate store instances** (as two
Container App replicas would have), plus the NullPool / event-loop requirement.
"""

from __future__ import annotations

import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

DSN = os.getenv("POSTGRES_TEST_DSN")

_has_driver = True
try:  # pragma: no cover - environment dependent
    import asyncpg  # noqa: F401
except Exception:  # noqa: BLE001
    _has_driver = False

pytestmark = pytest.mark.skipif(
    not (DSN and _has_driver),
    reason="PostgreSQL not configured (set POSTGRES_TEST_DSN and install asyncpg)",
)


def _engine():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    return create_async_engine(DSN, poolclass=NullPool)


@pytest.fixture
def stores():
    """Two independent store instances (separate engines) over one table."""
    import asyncio

    from dmer_common.messaging import PostgresIdempotencyStore
    from dmer_common.messaging.postgres_idempotency import metadata

    first, second = _engine(), _engine()

    async def create():
        async with first.begin() as conn:
            await conn.run_sync(metadata.create_all)

    asyncio.run(create())
    yield PostgresIdempotencyStore(first), PostgresIdempotencyStore(second)
    asyncio.run(first.dispose())
    asyncio.run(second.dispose())


def _mid() -> str:
    return f"m-{uuid.uuid4()}"  # unique per test run: re-runnable on one database


def test_duplicate_suppressed_across_store_instances(stores):
    a, b = stores
    mid = _mid()
    claim = a.claim("di-processor/dmer-raw", mid)
    assert claim.outcome.value == "CLAIMED"

    # another instance sees it in progress while the claim is live
    assert b.claim("di-processor/dmer-raw", mid).outcome.value == "IN_PROGRESS"

    assert a.complete("di-processor/dmer-raw", mid, claim.token) is True
    # ... and as a duplicate once completed (durably, via the table)
    assert b.claim("di-processor/dmer-raw", mid).outcome.value == "DUPLICATE"


def test_scopes_are_independent(stores):
    a, b = stores
    mid = _mid()
    token = a.claim("di-processor/dmer-raw", mid).token
    a.complete("di-processor/dmer-raw", mid, token)
    assert b.claim("orchestrator/dmer-extracted", mid).outcome.value == "CLAIMED"


def test_concurrent_claims_exactly_one_wins():
    # Fresh (cold) engines on purpose: also proves the first-connect serialization
    # keeps concurrent first use from deadlocking.
    import asyncio

    from dmer_common.messaging import PostgresIdempotencyStore
    from dmer_common.messaging.postgres_idempotency import metadata

    warm = _engine()

    async def create():
        async with warm.begin() as conn:
            await conn.run_sync(metadata.create_all)

    asyncio.run(create())
    asyncio.run(warm.dispose())
    a, b = PostgresIdempotencyStore(_engine()), PostgresIdempotencyStore(_engine())
    mid = _mid()
    # 8 claimers across two store instances, racing on separate threads
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(
            pool.map(
                lambda i: (a if i % 2 else b).claim("s", mid).outcome.value, range(8)
            )
        )
    assert outcomes.count("CLAIMED") == 1
    assert outcomes.count("IN_PROGRESS") == 7


def test_release_makes_message_claimable_again(stores):
    a, b = stores
    mid = _mid()
    token = a.claim("s", mid).token
    a.release("s", mid, token)  # handler failed
    assert b.claim("s", mid).outcome.value == "CLAIMED"


def test_expired_claim_is_taken_over_and_old_token_is_fenced():
    import asyncio

    from dmer_common.messaging import PostgresIdempotencyStore
    from dmer_common.messaging.postgres_idempotency import metadata

    engine = _engine()

    async def create():
        async with engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

    asyncio.run(create())
    try:
        short = PostgresIdempotencyStore(engine, lease_seconds=1)
        mid = _mid()
        old = short.claim("s", mid).token  # worker crashes, never completes
        assert short.claim("s", mid).outcome.value == "IN_PROGRESS"
        time.sleep(1.5)
        takeover = short.claim("s", mid)
        assert takeover.outcome.value == "CLAIMED"
        # the crashed worker's token can no longer complete or release it
        assert short.complete("s", mid, old) is False
        short.release("s", mid, old)
        assert short.complete("s", mid, takeover.token) is True
        assert short.claim("s", mid).outcome.value == "DUPLICATE"
    finally:
        asyncio.run(engine.dispose())


def test_store_rejects_a_pooled_engine():
    from dmer_common.messaging import PostgresIdempotencyStore
    from sqlalchemy.ext.asyncio import create_async_engine

    with pytest.raises(TypeError, match="NullPool"):
        PostgresIdempotencyStore(create_async_engine(DSN))


def test_nullpool_engine_survives_one_event_loop_per_message():
    # Regression: the pipeline runs each message in its own asyncio.run; a
    # pooled engine failed from the second message on.
    import asyncio

    from sqlalchemy import text

    engine = _engine()

    async def one():
        async with engine.connect() as conn:
            return (await conn.execute(text("select 1"))).scalar_one()

    assert [asyncio.run(one()) for _ in range(3)] == [1, 1, 1]
