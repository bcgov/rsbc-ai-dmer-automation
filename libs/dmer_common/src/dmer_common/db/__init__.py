"""PostgreSQL access layer for the DMER pipeline.

Exposes table repositories and the processing-status state machine.
Persistence uses SQLAlchemy Core with an async engine (Managed Identity token
auth is supplied by the caller via the connection URL/credential); pure logic
(state-transition rules, licence-number normalization) is unit-testable
without a live database.

``documents``/``DocumentRepository``/``DocumentStatus`` are the *original*
architecture's di-processor table -- kept as-is for that service, not part of
the revised architecture's Ingest stage. ``dmer_document``/``driver``/
``poll_checkpoint`` are the revised architecture's tables (see
``docs/development/data-model.md``).
"""

from __future__ import annotations

from .dmer_document import DmerDocumentRecord, DmerDocumentRepository
from .dmer_stage_run import DmerStageRunRecord, DmerStageRunRepository
from .documents import DocumentRecord, DocumentRepository
from .driver import DriverRecord, DriverRepository, normalize_licence_number
from .poll_checkpoint import PollCheckpointRecord, PollCheckpointRepository
from .status import DocumentStatus, is_valid_transition, next_statuses

__all__ = [
    "DmerDocumentRecord",
    "DmerDocumentRepository",
    "DmerStageRunRecord",
    "DmerStageRunRepository",
    "DocumentRecord",
    "DocumentRepository",
    "DocumentStatus",
    "DriverRecord",
    "DriverRepository",
    "PollCheckpointRecord",
    "PollCheckpointRepository",
    "is_valid_transition",
    "next_statuses",
    "normalize_licence_number",
]
