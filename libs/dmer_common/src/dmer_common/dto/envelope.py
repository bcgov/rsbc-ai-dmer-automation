"""Standard Service Bus message envelope shared by every queue message.

Under the revised architecture (see ``docs/development/message-contracts.md``)
**one message shape covers all four pipeline queues** (``dmer-ingest``,
``dmer-raw``, ``dmer-extracted``, ``driver-decision``). Messages carry *pointers,
never payloads*: identifiers plus a ``blob_url`` pointing at the artifact the next
stage needs — never an extracted/normalized document body inline.

Fields are declared in ``snake_case`` (Python convention) but serialize to and
deserialize from ``camelCase`` on the wire (contract convention), e.g.
``message_id`` <-> ``messageId``. ``message_id`` is the idempotency key: every
consumer must no-op (not error) on a ``message_id`` it has already completed.

There is no separate ``correlation_id`` field: ``document_id`` (internal
``dmer_document.id``, generated once at ingest, constant for the document's
life -- see ``docs/development/data-model.md``) already serves that role, so
a second id for the same purpose would be redundant.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Envelope(BaseModel):
    """Base for all pipeline messages.

    Carries the three envelope fields every message shares (``message_id``,
    ``document_id``, ``schema_version``). ``message_id`` is the idempotency key;
    ``document_id`` is the tracing/join key used on every log line and DB join.
    Concrete queue messages add the entity-specific pointer fields (see
    :mod:`dmer_common.dto.messages`).
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    message_id: str
    document_id: str
    schema_version: str
