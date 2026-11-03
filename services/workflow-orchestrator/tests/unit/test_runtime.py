"""run_sync: blocking calls run in a thread with the caller's context."""

from __future__ import annotations

import asyncio

from dmer_common.telemetry import document_id_context
from dmer_common.telemetry.logging import get_document_id
from workflow_orchestrator.activities._runtime import run_sync


def test_run_sync_carries_the_bound_document_id_into_the_thread():
    # Normalization logs from inside run_sync; without the caller's context
    # they were written with document_id null (dev, 2026-10-05).
    async def main():
        with document_id_context("doc-1"):
            return await run_sync(get_document_id)

    assert asyncio.run(main()) == "doc-1"


def test_run_sync_passes_arguments_through():
    async def main():
        return await run_sync(lambda a, *, b: a + b, 1, b=2)

    assert asyncio.run(main()) == 3
