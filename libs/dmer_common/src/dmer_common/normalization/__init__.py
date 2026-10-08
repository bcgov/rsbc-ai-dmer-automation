"""DMER normalization -- Document Orchestration's Activity: Normalize.

See docs/development/stages/04-activity-normalize.md. The activity itself
(the thin Durable Functions trigger in services/workflow-orchestrator/)
should only need :func:`normalize_document`, :data:`NORMALIZATION_SCHEMA_VERSION`,
and :class:`NormalizationValidationError` from this package -- everything
else here is implementation detail, exposed for direct unit testing.
"""

from __future__ import annotations

from .pipeline import (
    NORMALIZATION_SCHEMA_VERSION,
    NormalizationValidationError,
    adapt_combined_fields,
    normalize_document,
    parse_cognitive_score,
    parse_guide_section,
    parse_nyha_class,
)
from .schema import CONDITIONS, ConditionCategory

__all__ = [
    "CONDITIONS",
    "NORMALIZATION_SCHEMA_VERSION",
    "ConditionCategory",
    "NormalizationValidationError",
    "adapt_combined_fields",
    "normalize_document",
    "parse_cognitive_score",
    "parse_guide_section",
    "parse_nyha_class",
]
