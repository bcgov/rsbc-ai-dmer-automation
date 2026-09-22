"""Standard Service Bus message envelope shared by every queue/topic message.

Every message in the DMER pipeline carries the same three envelope fields
(``message_id``, ``correlation_id``, ``schema_version``) plus entity-specific
fields. This base model defines those common fields and the JSON (camelCase)
serialization used on the wire, matching the contracts in
``docs/contracts/queues/``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class Envelope(BaseModel):
    """Base for all pipeline messages.

    Fields are declared in ``snake_case`` (Python convention) but serialize to
    and deserialize from ``camelCase`` on the wire (contract convention), e.g.
    ``message_id`` <-> ``messageId``. ``message_id`` is the idempotency key.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )

    message_id: str
    correlation_id: str
    schema_version: str
