"""Blob Storage helpers: managed-identity-authenticated client and path builders
for the pipeline container layout (raw/ocr/normalized/rules/audit/failed/archive
plus the di-processor ``extracted-dmer`` container that holds all four extraction
artifacts per document)."""

from . import containers
from .client import BlobClient
from .containers import extracted_dmer, normalized_dmer, rules, rules_active_path
from .paths import (
    combined_path,
    handwritten_path,
    normalized_path,
    ocr_path,
    top_level_path,
)

__all__ = [
    "BlobClient",
    "combined_path",
    "containers",
    "extracted_dmer",
    "handwritten_path",
    "normalized_dmer",
    "normalized_path",
    "ocr_path",
    "rules",
    "rules_active_path",
    "top_level_path",
]
