"""Queue message models for the DMER pipeline.

Single source of truth for the message shapes documented in
``docs/development/message-contracts.md``. Under the revised architecture **one
envelope shape covers all four queues** — pointers only, never extracted or
normalized content inline:

- :class:`RawMessage` — ``dmer-raw`` (produced by the Ingest function, consumed
  by ``di-processor``).
- :class:`ExtractedMessage` — ``dmer-extracted`` (produced by ``di-processor``,
  consumed by the document orchestrator).

Both are thin subclasses of :class:`PipelineMessage`: the queue a message belongs
to is context, not shape. ``schema_version`` defaults to
:data:`PIPELINE_SCHEMA_VERSION`.

Security: never place a licence number or clinical content in a message body —
carry ``driver_key`` and a ``blob_url`` only (see message-contracts.md §Security).
"""

from __future__ import annotations

from datetime import datetime

from .envelope import Envelope

PIPELINE_SCHEMA_VERSION = "1.0"


class PipelineMessage(Envelope):
    """The one message shape carried on every pipeline queue.

    See ``docs/development/message-contracts.md`` §Message envelope. ``blob_url``
    points at the artifact the *next* stage needs (e.g. the source PDF under the
    ``raw-dmer`` container for ``dmer-raw``; the combined extraction under
    ``extracted-dmer`` for ``dmer-extracted``). ``message_id`` is the idempotency
    key.
    """

    schema_version: str = PIPELINE_SCHEMA_VERSION
    document_id: str
    document_guid: str
    driver_key: str | None = None
    blob_url: str
    attempt: int = 1
    enqueued_at: datetime


class RawMessage(PipelineMessage):
    """``dmer-raw`` message — a document awaiting extraction.

    ``blob_url`` points at the source PDF under the ``raw-dmer`` container.
    ``driver_key`` may be null: Ingest supplies it only when Mercury returned a
    driver object; otherwise Extraction resolves it (deferred — see the
    di-processor spec).
    """


class ExtractedMessage(PipelineMessage):
    """``dmer-extracted`` message — a document whose extraction is published.

    ``blob_url`` points at the combined extraction JSON under the
    ``extracted-dmer`` container; the document orchestrator reads it to start the
    per-document durable orchestration.
    """
