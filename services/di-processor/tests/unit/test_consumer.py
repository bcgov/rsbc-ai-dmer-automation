"""Unit tests for the thin consumer adapter (Requirements 3.1, 3.5).

Asserts the handler parses the envelope into a RawMessage and drives the
async pipeline, and that failures propagate so the shared consumer dead-letters.
Written as GIVEN/WHEN/THEN behaviour specs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from di_processor.consumer import make_handler
from dmer_common.dto import RawMessage


class _RecordingPipeline:
    """Stand-in exposing the async ``run`` the adapter drives."""

    def __init__(self, *, boom: bool = False):
        self.calls: list[RawMessage] = []
        self._boom = boom

    async def run(self, message: RawMessage) -> None:
        self.calls.append(message)
        if self._boom:
            raise RuntimeError("pipeline failed")


def _envelope(**over) -> dict:
    base = {
        "messageId": "m-1",
        "correlationId": "case-1",
        "schemaVersion": "1.0",
        "documentId": "doc-1",
        "documentGuid": "123e4567-e89b-12d3-a456-426614174000",
        "driverKey": None,
        "blobUrl": "https://acct.blob.core.windows.net/raw-dmer/doc-1.pdf",
        "attempt": 1,
        "enqueuedAt": datetime(2026, 8, 5, tzinfo=UTC).isoformat(),
    }
    base.update(over)
    return base


def test_handler_parses_envelope_and_runs_pipeline():
    """GIVEN an envelope WHEN the handler runs THEN the pipeline gets the DTO."""
    pipeline = _RecordingPipeline()
    handler = make_handler(pipeline)  # type: ignore[arg-type]

    handler(_envelope())

    assert len(pipeline.calls) == 1
    assert pipeline.calls[0].document_id == "doc-1"
    assert pipeline.calls[0].correlation_id == "case-1"


def test_handler_propagates_pipeline_failure():
    """GIVEN the pipeline raises WHEN the handler runs THEN the error propagates."""
    pipeline = _RecordingPipeline(boom=True)
    handler = make_handler(pipeline)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError):
        handler(_envelope())
