"""Shared Pydantic message/data-transfer objects (the single source of truth for
the queue message shapes documented in docs/development/message-contracts.md).

Under the revised architecture one envelope shape covers all four pipeline queues;
``RawMessage`` (``dmer-raw``) and ``ExtractedMessage`` (``dmer-extracted``) are the
two di-processor cares about."""

from .envelope import Envelope
from .messages import (
    DMER_EVENT_NAMESPACE,
    EXTRACTED_EVENT,
    PIPELINE_SCHEMA_VERSION,
    ExtractedMessage,
    PipelineMessage,
    RawMessage,
    event_message_id,
)

__all__ = [
    "DMER_EVENT_NAMESPACE",
    "EXTRACTED_EVENT",
    "PIPELINE_SCHEMA_VERSION",
    "Envelope",
    "ExtractedMessage",
    "PipelineMessage",
    "RawMessage",
    "event_message_id",
]
