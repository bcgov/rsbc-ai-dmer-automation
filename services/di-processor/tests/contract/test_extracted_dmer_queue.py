"""Contract test: ``extracted-dmer-queue`` v2 envelope (Requirements 7.3, 7.4).

di-processor is the producer; ``workflow-orchestrator`` is the consumer. This
locks the wire shape of ``dmer_common.dto.ExtractedDmerMessage`` to the v2
contract documented in ``docs/contracts/queues/extracted-dmer-queue.md``:
``schemaVersion`` 2.0, ``combinedResultUri`` (replacing the v1 ``ocrResultUri``),
and the standard camelCase envelope. Written GIVEN/WHEN/THEN.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from dmer_common.dto import EXTRACTED_DMER_SCHEMA_VERSION, ExtractedDmerMessage
from pydantic import ValidationError

# The exact field set the documented v2 envelope carries (camelCase on the wire).
V2_ENVELOPE_KEYS = {
    "messageId",
    "correlationId",
    "schemaVersion",
    "documentId",
    "mercuryCaseId",
    "sha256Hash",
    "combinedResultUri",
    "processedAt",
}


def _message() -> ExtractedDmerMessage:
    return ExtractedDmerMessage(
        message_id="m-1",
        correlation_id="case-1",
        document_id="doc-1",
        mercury_case_id="case-1",
        sha256_hash="abc123",
        combined_result_uri=(
            "https://acct.blob.core.windows.net/combined-extracted-dmer/doc-1/combined.json"
        ),
        processed_at=datetime(2026, 8, 5, 12, 5, tzinfo=UTC),
    )


def test_schema_version_is_2_0():
    """GIVEN the v2 DTO WHEN serialized THEN schemaVersion is 2.0."""
    assert EXTRACTED_DMER_SCHEMA_VERSION == "2.0"
    wire = json.loads(_message().model_dump_json(by_alias=True))
    assert wire["schemaVersion"] == "2.0"


def test_wire_shape_matches_documented_v2_envelope():
    """GIVEN the v2 DTO WHEN serialized by alias THEN the exact documented
    camelCase key set is emitted, with combinedResultUri and no ocrResultUri."""
    wire = json.loads(_message().model_dump_json(by_alias=True))

    assert set(wire) == V2_ENVELOPE_KEYS
    assert "combinedResultUri" in wire
    assert "ocrResultUri" not in wire  # v1 field is gone (breaking rename)
    assert wire["combinedResultUri"].endswith("/combined.json")


def test_round_trips_from_wire_envelope():
    """GIVEN a documented v2 wire envelope WHEN validated THEN it deserializes."""
    wire = _message().model_dump_json(by_alias=True)
    parsed = ExtractedDmerMessage.model_validate_json(wire)
    assert parsed.combined_result_uri.endswith("/combined.json")
    assert parsed.schema_version == "2.0"


def test_rejects_v1_field_name():
    """GIVEN a v1 envelope (ocrResultUri) WHEN validated THEN it is rejected
    (extra fields forbidden + combinedResultUri missing)."""
    v1 = {
        "messageId": "m-1",
        "correlationId": "case-1",
        "schemaVersion": "1.0",
        "documentId": "doc-1",
        "mercuryCaseId": "case-1",
        "sha256Hash": "abc123",
        "ocrResultUri": "https://acct.blob.core.windows.net/ocr/doc-1/ocr.json",
        "processedAt": "2026-08-05T12:05:00Z",
    }
    with pytest.raises(ValidationError):
        ExtractedDmerMessage.model_validate(v1)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
