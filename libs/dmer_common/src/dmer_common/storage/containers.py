"""Blob container names for the di-processor extraction stage and the Document
Orchestration stage's Normalize activity.

Under the revised architecture (see ``docs/development/stages/02-extraction.md``)
extraction writes to a **single** container, ``extracted-dmer``. All four
per-document artifacts (top-level, OCR, handwritten, combined) live under this one
container, namespaced by document id (see :mod:`dmer_common.storage.paths`).
Normalize writes to ``normalized-dmer`` (see
``docs/development/stages/04-activity-normalize.md``).

Names are configurable via ``EXTRACTED_DMER_CONTAINER``,
``NORMALIZED_DMER_CONTAINER``, ``RULES_CONTAINER`` and ``RULES_ACTIVE_PATH``
(sourced from App Configuration in Azure) so they can change without a code
change; the defaults match the provisioned containers.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_EXTRACTED_DMER: Final = "extracted-dmer"
DEFAULT_NORMALIZED_DMER: Final = "normalized-dmer"
DEFAULT_RULES: Final = "rules"
DEFAULT_RULES_ACTIVE_PATH: Final = "active/rules.json"


def extracted_dmer() -> str:
    """Container for all extraction artifacts (top_level/ocr/handwritten/combined)."""
    return os.getenv("EXTRACTED_DMER_CONTAINER", DEFAULT_EXTRACTED_DMER)


def normalized_dmer() -> str:
    """Container for the Normalize activity's output (see
    docs/development/stages/04-activity-normalize.md)."""
    return os.getenv("NORMALIZED_DMER_CONTAINER", DEFAULT_NORMALIZED_DMER)


def rules() -> str:
    """Container holding the published rulesets (see
    docs/development/services/azure-blob-storage.md#versioning-rules)."""
    return os.getenv("RULES_CONTAINER", DEFAULT_RULES)


def rules_active_path() -> str:
    """Path of the active ruleset within :func:`rules` -- ``rules/active/rules.json``
    in the docs' container-prefixed notation."""
    return os.getenv("RULES_ACTIVE_PATH", DEFAULT_RULES_ACTIVE_PATH).lstrip("/")
