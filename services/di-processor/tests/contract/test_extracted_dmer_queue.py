"""Contract test: ``dmer-extracted`` message envelope.

di-processor is the producer; the document orchestrator is the consumer. This
locks the wire shape of ``dmer_common.dto.ExtractedMessage`` to the unified
pipeline envelope documented in ``docs/development/message-contracts.md``:
one shape across all four queues, camelCase on the wire, ``blobUrl`` pointing at
the combined extraction under the ``extracted-dmer`` container. Written
GIVEN/WHEN/THEN.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from dmer_common.dto import PIPELINE_SCHEMA_VERSION, ExtractedMessage
from pydantic import ValidationError

# The exact field set the documented unified envelope carries (camelCase on wire).
ENVELOPE_KEYS = {
    "messageId",
    "correlationId",
    "schemaVersion",
    "documentId",
    "documentGuid",
    "driverKey",
    "blobUrl",
    "attempt",
    "enqueuedAt",
}


def _message() -> ExtractedMessage:
    return ExtractedMessage(
        message_id="m-1",
        correlation_id="case-1",
        document_id="doc-1",
        document_guid="123e4567-e89b-12d3-a456-426614174000",
        driver_key="a91b77e4-0000-0000-0000-000000000000",
        blob_url=(
            "https://acct.blob.core.windows.net/extracted-dmer/doc-1/combined.json"
        ),
        enqueued_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
    )


def test_schema_version_default():
    """GIVEN the DTO WHEN serialized THEN schemaVersion is the pipeline default."""
    wire = json.loads(_message().model_dump_json(by_alias=True))
    assert wire["schemaVersion"] == PIPELINE_SCHEMA_VERSION


def test_wire_shape_matches_documented_envelope():
    """GIVEN the DTO WHEN serialized by alias THEN the exact documented
    camelCase key set is emitted, with blobUrl at the combined extraction."""
    wire = json.loads(_message().model_dump_json(by_alias=True))

    assert set(wire) == ENVELOPE_KEYS
    assert wire["blobUrl"].endswith("/combined.json")
    assert "/extracted-dmer/" in wire["blobUrl"]


def test_round_trips_from_wire_envelope():
    """GIVEN a documented wire envelope WHEN validated THEN it deserializes."""
    wire = _message().model_dump_json(by_alias=True)
    parsed = ExtractedMessage.model_validate_json(wire)
    assert parsed.blob_url.endswith("/combined.json")
    assert parsed.document_guid == "123e4567-e89b-12d3-a456-426614174000"


def test_rejects_unknown_field():
    """GIVEN an envelope with an unexpected field WHEN validated THEN it is
    rejected (extra fields forbidden — guards contract drift)."""
    wire = {
        "messageId": "m-1",
        "correlationId": "case-1",
        "schemaVersion": "1.0",
        "documentId": "doc-1",
        "documentGuid": "123e4567-e89b-12d3-a456-426614174000",
        "driverKey": None,
        "blobUrl": "https://acct.blob.core.windows.net/extracted-dmer/doc-1/combined.json",
        "attempt": 1,
        "enqueuedAt": "2026-08-05T12:05:00Z",
        "ocrResultUri": "https://legacy/field",
    }
    with pytest.raises(ValidationError):
        ExtractedMessage.model_validate(wire)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
