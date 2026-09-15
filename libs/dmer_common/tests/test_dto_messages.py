"""Tests for the shared queue-message DTOs.

Behaviour specs (GIVEN/WHEN/THEN) for envelope (de)serialization, camelCase
wire format, required-field validation, and idempotency-key presence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from dmer_common.dto import (
    EXTRACTED_DMER_SCHEMA_VERSION,
    ExtractedDmerMessage,
    RawDmerMessage,
)

RAW_WIRE = {
    "messageId": "11111111-1111-1111-1111-111111111111",
    "correlationId": "case-123",
    "schemaVersion": "1.0",
    "sourceSystem": "mercury-webhook",
    "documentId": "doc-1",
    "mercuryCaseId": "case-123",
    "documentUri": "https://example/doc.pdf",
    "receivedAt": "2026-08-05T12:00:00Z",
    "payload": {"foo": "bar"},
}

EXTRACTED_WIRE = {
    "messageId": "22222222-2222-2222-2222-222222222222",
    "correlationId": "case-123",
    "schemaVersion": "2.0",
    "documentId": "doc-1",
    "mercuryCaseId": "case-123",
    "sha256Hash": "abc123",
    "combinedResultUri": "combined-extracted-dmer/doc-1/combined.json",
    "processedAt": "2026-08-05T12:05:00Z",
}


def test_raw_message_deserializes_from_camelcase_wire():
    # GIVEN a raw-dmer-queue payload in camelCase
    # WHEN parsed into RawDmerMessage
    msg = RawDmerMessage.model_validate(RAW_WIRE)
    # THEN fields are populated and message_id is the idempotency key
    assert msg.message_id == RAW_WIRE["messageId"]
    assert msg.source_system == "mercury-webhook"
    assert msg.payload == {"foo": "bar"}
    assert isinstance(msg.received_at, datetime)


def test_raw_message_round_trips_to_camelcase():
    # GIVEN a parsed raw message
    msg = RawDmerMessage.model_validate(RAW_WIRE)
    # WHEN serialized back with aliases
    dumped = msg.model_dump(by_alias=True, mode="json")
    # THEN it reproduces the camelCase wire keys
    assert dumped["messageId"] == RAW_WIRE["messageId"]
    assert dumped["sourceSystem"] == "mercury-webhook"
    assert "source_system" not in dumped


def test_extracted_message_defaults_schema_version_and_serializes():
    # GIVEN an extracted message built from Python (no schema_version given)
    msg = ExtractedDmerMessage(
        message_id="22222222-2222-2222-2222-222222222222",
        correlation_id="case-123",
        document_id="doc-1",
        mercury_case_id="case-123",
        sha256_hash="abc123",
        combined_result_uri="combined-extracted-dmer/doc-1/combined.json",
        processed_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
    )
    # THEN schema_version defaults to v2 and serializes as combinedResultUri
    assert msg.schema_version == EXTRACTED_DMER_SCHEMA_VERSION
    dumped = msg.model_dump(by_alias=True, mode="json")
    assert dumped["schemaVersion"] == "2.0"
    assert dumped["combinedResultUri"].endswith("combined.json")
    assert "ocrResultUri" not in dumped


def test_extracted_message_deserializes_from_wire():
    # GIVEN the v2 extracted-dmer wire payload
    # WHEN parsed
    msg = ExtractedDmerMessage.model_validate(EXTRACTED_WIRE)
    # THEN the combined result URI is captured
    assert msg.combined_result_uri.endswith("combined.json")


def test_missing_required_field_raises():
    # GIVEN a payload missing the idempotency key
    bad = {k: v for k, v in RAW_WIRE.items() if k != "messageId"}
    # WHEN parsed THEN validation fails
    with pytest.raises(ValidationError):
        RawDmerMessage.model_validate(bad)


def test_unknown_field_is_rejected():
    # GIVEN a payload with an unexpected field
    bad = {**EXTRACTED_WIRE, "unexpected": "nope"}
    # WHEN parsed THEN validation fails (extra=forbid guards contract drift)
    with pytest.raises(ValidationError):
        ExtractedDmerMessage.model_validate(bad)
