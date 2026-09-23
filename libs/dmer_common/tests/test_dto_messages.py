"""Tests for the shared queue-message DTOs.

Behaviour specs (GIVEN/WHEN/THEN) for the unified pipeline envelope: camelCase
wire (de)serialization, required-field validation, idempotency-key presence, and
the shared shape across ``dmer-raw`` (:class:`RawMessage`) and ``dmer-extracted``
(:class:`ExtractedMessage`).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dmer_common.dto import (
    PIPELINE_SCHEMA_VERSION,
    ExtractedMessage,
    RawMessage,
)
from pydantic import ValidationError

RAW_WIRE = {
    "messageId": "11111111-1111-1111-1111-111111111111",
    "correlationId": "case-123",
    "schemaVersion": "1.0",
    "documentId": "doc-1",
    "documentGuid": "123e4567-e89b-12d3-a456-426614174000",
    "driverKey": None,
    "blobUrl": "https://example/raw-dmer/doc-1.pdf",
    "attempt": 1,
    "enqueuedAt": "2026-08-05T12:00:00Z",
}

EXTRACTED_WIRE = {
    "messageId": "22222222-2222-2222-2222-222222222222",
    "correlationId": "case-123",
    "schemaVersion": "1.0",
    "documentId": "doc-1",
    "documentGuid": "123e4567-e89b-12d3-a456-426614174000",
    "driverKey": "a91b77e4-0000-0000-0000-000000000000",
    "blobUrl": "https://example/extracted-dmer/doc-1/combined.json",
    "attempt": 1,
    "enqueuedAt": "2026-08-05T12:05:00Z",
}


def test_raw_message_deserializes_from_camelcase_wire():
    # GIVEN a dmer-raw payload in camelCase
    # WHEN parsed into RawMessage
    msg = RawMessage.model_validate(RAW_WIRE)
    # THEN fields are populated and message_id is the idempotency key
    assert msg.message_id == RAW_WIRE["messageId"]
    assert msg.document_guid == RAW_WIRE["documentGuid"]
    assert msg.driver_key is None
    assert msg.blob_url.endswith("doc-1.pdf")
    assert isinstance(msg.enqueued_at, datetime)


def test_raw_message_round_trips_to_camelcase():
    # GIVEN a parsed raw message
    msg = RawMessage.model_validate(RAW_WIRE)
    # WHEN serialized back with aliases
    dumped = msg.model_dump(by_alias=True, mode="json")
    # THEN it reproduces the camelCase wire keys
    assert dumped["messageId"] == RAW_WIRE["messageId"]
    assert dumped["documentGuid"] == RAW_WIRE["documentGuid"]
    assert "document_guid" not in dumped


def test_extracted_message_defaults_schema_version_and_serializes():
    # GIVEN an extracted message built from Python (no schema_version given)
    msg = ExtractedMessage(
        message_id="22222222-2222-2222-2222-222222222222",
        correlation_id="case-123",
        document_id="doc-1",
        document_guid="123e4567-e89b-12d3-a456-426614174000",
        driver_key="a91b77e4-0000-0000-0000-000000000000",
        blob_url="https://example/extracted-dmer/doc-1/combined.json",
        enqueued_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
    )
    # THEN schema_version defaults and blob_url points at the combined extraction
    assert msg.schema_version == PIPELINE_SCHEMA_VERSION
    dumped = msg.model_dump(by_alias=True, mode="json")
    assert dumped["schemaVersion"] == PIPELINE_SCHEMA_VERSION
    assert dumped["blobUrl"].endswith("combined.json")


def test_extracted_message_deserializes_from_wire():
    # GIVEN the dmer-extracted wire payload
    # WHEN parsed
    msg = ExtractedMessage.model_validate(EXTRACTED_WIRE)
    # THEN the combined-extraction blob URL is captured
    assert msg.blob_url.endswith("combined.json")
    assert msg.driver_key == EXTRACTED_WIRE["driverKey"]


def test_missing_required_field_raises():
    # GIVEN a payload missing the idempotency key
    bad = {k: v for k, v in RAW_WIRE.items() if k != "messageId"}
    # WHEN parsed THEN validation fails
    with pytest.raises(ValidationError):
        RawMessage.model_validate(bad)


def test_unknown_field_is_rejected():
    # GIVEN a payload with an unexpected field
    bad = {**EXTRACTED_WIRE, "unexpected": "nope"}
    # WHEN parsed THEN validation fails (extra=forbid guards contract drift)
    with pytest.raises(ValidationError):
        ExtractedMessage.model_validate(bad)


def test_event_message_id_is_deterministic_per_event():
    import uuid

    from dmer_common.dto import EXTRACTED_EVENT, event_message_id

    first = event_message_id(EXTRACTED_EVENT, "doc-1")
    # same event + key -> same id (a replay republishes the same event)
    assert event_message_id(EXTRACTED_EVENT, "doc-1") == first
    # a valid UUID
    assert str(uuid.UUID(first)) == first
    # different document or different event -> different id
    assert event_message_id(EXTRACTED_EVENT, "doc-2") != first
    assert event_message_id("dmer-normalized", "doc-1") != first


def test_event_message_id_is_stable_across_releases():
    # Pinned: changing the namespace or format would re-key every event and
    # break downstream idempotency for anything already published.
    from dmer_common.dto import EXTRACTED_EVENT, event_message_id

    assert event_message_id(EXTRACTED_EVENT, "doc-1") == PINNED_EXTRACTED_DOC_1


# event_message_id("dmer-extracted", "doc-1") — pinned; see the stability test.
PINNED_EXTRACTED_DOC_1 = "5893ac38-40b3-5070-bb1e-0236fb1fd093"
