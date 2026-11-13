"""Blob container names for the di-processor extraction stage and the Document
Orchestration stage's Normalize activity.

Under the revised architecture (see ``docs/development/stages/02-extraction.md``)
extraction writes to a **single** container, ``extracted-dmer``. All four
per-document artifacts (top-level, OCR, handwritten, combined) live under this one
container, namespaced by document id (see :mod:`dmer_common.storage.paths`).
Normalize writes to ``normalized-dmer`` (see
``docs/development/stages/04-activity-normalize.md``).

The triage LLM reads the procedures PDF from ``rules`` (beside the ruleset,
so it is maintained the same way) and keeps the text generated from it in
``triage-policy`` -- the one place the pipeline may write, so ``rules`` stays
read-only to it.

Names are configurable via ``EXTRACTED_DMER_CONTAINER``,
``NORMALIZED_DMER_CONTAINER``, ``RULES_CONTAINER``, ``RULES_ACTIVE_PATH``,
``TRIAGE_POLICY_PDF_PATH`` and ``TRIAGE_POLICY_CONTAINER`` (sourced from App
Configuration in Azure) so they can change without a code change; the
defaults match the provisioned containers.
"""

from __future__ import annotations

import os
from typing import Final

DEFAULT_EXTRACTED_DMER: Final = "extracted-dmer"
DEFAULT_NORMALIZED_DMER: Final = "normalized-dmer"
DEFAULT_RULES: Final = "rules"
DEFAULT_RULES_ACTIVE_PATH: Final = "active/rules.json"
DEFAULT_TRIAGE_POLICY_PDF_PATH: Final = "active/triage_sort_procedures.pdf"
DEFAULT_TRIAGE_POLICY: Final = "triage-policy"


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


def triage_policy_pdf_path() -> str:
    """Path of the active Triage Sort Procedures PDF within :func:`rules` --
    the triage LLM's policy, maintained like the ruleset beside it."""
    return os.getenv("TRIAGE_POLICY_PDF_PATH", DEFAULT_TRIAGE_POLICY_PDF_PATH).lstrip(
        "/"
    )


def triage_policy() -> str:
    """Container for the text generated from the procedures PDF, one blob per
    PDF version (``<pdf sha256>.txt``)."""
    return os.getenv("TRIAGE_POLICY_CONTAINER", DEFAULT_TRIAGE_POLICY)
