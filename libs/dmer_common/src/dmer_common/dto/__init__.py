"""Shared Pydantic message/data-transfer objects (the single source of truth for
the queue message shapes documented in docs/development/message-contracts.md).

Under the revised architecture one envelope shape covers all four pipeline queues;
``RawMessage`` (``dmer-raw``) and ``ExtractedMessage`` (``dmer-extracted``) are the
two di-processor cares about."""

from .envelope import Envelope
from .messages import (
    PIPELINE_SCHEMA_VERSION,
    ExtractedMessage,
    PipelineMessage,
    RawMessage,
)

__all__ = [
    "PIPELINE_SCHEMA_VERSION",
    "Envelope",
    "ExtractedMessage",
    "PipelineMessage",
    "RawMessage",
]
