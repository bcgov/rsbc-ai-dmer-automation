"""Blob container names for the di-processor extraction stage and the Document
Orchestration stage's Normalize activity.

Under the revised architecture (see ``docs/development/stages/02-extraction.md``)
extraction writes to a **single** container, ``extracted-dmer``. All four
per-document artifacts (top-level, OCR, handwritten, combined) live under this one
container, namespaced by document id (see :mod:`dmer_common.storage.paths`).
Normalize writes to ``normalized-dmer`` (see
``docs/development/stages/04-activity-normalize.md``).

Names are configurable via ``EXTRACTED_DMER_CONTAINER`` and
``NORMALIZED_DMER_CONTAINER`` (sourced from App Configuration in Azure) so they can
change without a code change; the defaults match the provisioned containers.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_EXTRACTED_DMER: Final = "extracted-dmer"
DEFAULT_NORMALIZED_DMER: Final = "normalized-dmer"


def extracted_dmer() -> str:
    """Container for all extraction artifacts (top_level/ocr/handwritten/combined)."""
    return os.getenv("EXTRACTED_DMER_CONTAINER", DEFAULT_EXTRACTED_DMER)


def normalized_dmer() -> str:
    """Container for the Normalize activity's output (see
    docs/development/stages/04-activity-normalize.md)."""
    return os.getenv("NORMALIZED_DMER_CONTAINER", DEFAULT_NORMALIZED_DMER)
