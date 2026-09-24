"""PostgreSQL access layer for the DMER pipeline.

The schema is owned by the Flyway migrations in ``database/migrations/``; these
modules mirror it with SQLAlchemy Core tables and async repositories:

- ``dmer_document`` — the document row: Ingest's upsert/``DOWNLOADED`` writes
  and later stages' compare-and-set ``pipeline_status`` transitions
  (``current_stage`` + ``pipeline_status``, the two-field status model).
- ``dmer_extraction`` — queryable extracted values, cut-off flags, comparison
  hash.
- ``dmer_stage_run`` — the audit trail (one row per document, stage, attempt).
- ``driver`` — one row per canonical BC licence number.
- ``poll_checkpoint`` — the Page Poller's resume point.

Persistence uses SQLAlchemy Core with an async engine (Managed Identity token
auth is supplied by the caller via the connection URL/credential); the
state-transition rules are pure and unit-testable without a live database.
"""

from __future__ import annotations

from .dmer_document import (
    DmerDocumentRecord,
    DmerDocumentRepository,
    InvalidStatusTransition,
    StaleStatusError,
)
from .dmer_extraction import ExtractionRecord, ExtractionRepository
from .dmer_stage_run import DmerStageRunRecord, DmerStageRunRepository, StageRunStatus
from .driver import DriverRecord, DriverRepository, normalize_licence_number
from .poll_checkpoint import PollCheckpointRecord, PollCheckpointRepository
from .status import (
    PipelineStage,
    PipelineStatus,
    is_valid_transition,
    next_statuses,
)

__all__ = [
    "DmerDocumentRecord",
    "DmerDocumentRepository",
    "DmerStageRunRecord",
    "DmerStageRunRepository",
    "DriverRecord",
    "DriverRepository",
    "ExtractionRecord",
    "ExtractionRepository",
    "InvalidStatusTransition",
    "PipelineStage",
    "PipelineStatus",
    "PollCheckpointRecord",
    "PollCheckpointRepository",
    "StageRunStatus",
    "StaleStatusError",
    "is_valid_transition",
    "next_statuses",
    "normalize_licence_number",
]
