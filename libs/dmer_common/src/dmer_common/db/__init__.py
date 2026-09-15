"""PostgreSQL access layer for the DMER pipeline.

Exposes the ``documents``-table repository and the processing-status state
machine. Persistence uses SQLAlchemy Core with an async engine (Managed Identity
token auth is supplied by the caller via the connection URL/credential); the
state-transition rules are pure and unit-testable without a live database.
"""

from __future__ import annotations

from .documents import DocumentRecord, DocumentRepository
from .status import DocumentStatus, is_valid_transition, next_statuses

__all__ = [
    "DocumentRecord",
    "DocumentRepository",
    "DocumentStatus",
    "is_valid_transition",
    "next_statuses",
]
