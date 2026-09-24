"""PostgreSQL access layer for the DMER pipeline.

Exposes the ``dmer_document`` repository (two-field status model), the
``dmer_extraction`` repository (queryable extracted values + cut-off flags +
comparison hash), and the ``dmer_stage_run`` audit writer, plus the pipeline
status/stage state machine. Persistence uses SQLAlchemy Core with an async engine
(Managed Identity token auth is supplied by the caller via the connection
URL/credential); the state-transition rules are pure and unit-testable without a
live database.
"""

from __future__ import annotations

from .dmer_document import (
    DmerDocumentRecord,
    DmerDocumentRepository,
    InvalidStatusTransition,
    StaleStatusError,
)
from .dmer_extraction import ExtractionRecord, ExtractionRepository
from .stage_run import StageRunRepository, StageRunStatus
from .status import (
    PipelineStage,
    PipelineStatus,
    is_valid_transition,
    next_statuses,
)

__all__ = [
    "DmerDocumentRecord",
    "DmerDocumentRepository",
    "ExtractionRecord",
    "ExtractionRepository",
    "InvalidStatusTransition",
    "PipelineStage",
    "PipelineStatus",
    "StageRunRepository",
    "StageRunStatus",
    "StaleStatusError",
    "is_valid_transition",
    "next_statuses",
]
