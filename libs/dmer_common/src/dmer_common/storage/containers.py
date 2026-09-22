"""Blob container names used by the di-processor extraction stage.

Names are configurable via environment variables (sourced from App Configuration
in Azure) so they can be changed without a code change; the defaults match the
containers provisioned for di-processor (see ``.kiro/specs/di-processor/design.md``).

Environment variables:
- ``EXTRACTED_DMER_CONTAINER``          (default ``extracted-dmer``)
- ``COMBINED_EXTRACTED_DMER_CONTAINER`` (default ``combined-extracted-dmer``)
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_EXTRACTED_DMER: Final = "extracted-dmer"
DEFAULT_COMBINED_EXTRACTED_DMER: Final = "combined-extracted-dmer"


def extracted_dmer() -> str:
    """Container for per-stage extraction artifacts (top_level/ocr/handwritten)."""
    return os.getenv("EXTRACTED_DMER_CONTAINER", DEFAULT_EXTRACTED_DMER)


def combined_extracted_dmer() -> str:
    """Container for the final unified combined extraction JSON."""
    return os.getenv(
        "COMBINED_EXTRACTED_DMER_CONTAINER", DEFAULT_COMBINED_EXTRACTED_DMER
    )
