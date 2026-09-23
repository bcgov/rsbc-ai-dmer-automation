"""Blob container name for the di-processor extraction stage.

Under the revised architecture (see ``docs/development/stages/02-extraction.md``)
extraction writes to a **single** container, ``extracted-dmer``. All four
per-document artifacts (top-level, OCR, handwritten, combined) live under this one
container, namespaced by document id (see :mod:`dmer_common.storage.paths`).

The name is configurable via ``EXTRACTED_DMER_CONTAINER`` (sourced from App
Configuration in Azure) so it can change without a code change; the default
matches the provisioned container.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_EXTRACTED_DMER: Final = "extracted-dmer"


def extracted_dmer() -> str:
    """Container for all extraction artifacts (top_level/ocr/handwritten/combined)."""
    return os.getenv("EXTRACTED_DMER_CONTAINER", DEFAULT_EXTRACTED_DMER)
