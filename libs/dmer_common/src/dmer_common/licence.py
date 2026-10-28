"""BC driver's licence number normalization.

The one canonical form used everywhere a licence is stored or compared —
``driver.licence_number`` (written by Ingest from Mercury) and
``dmer_extraction.licence_number_read`` (written by Extraction from the page)
must normalize identically, or the same driver would never match.

BC licence numbers are **digits only, 7 or 8 long**: 8-digit numbers have been
issued since July 2023; older 7-digit numbers remain valid and are commonly
stored with a leading ``0`` in backend systems. The canonical form is therefore
**8 digits**, zero-padding a 7-digit number, so ``1234567`` and ``01234567`` are
the same driver.

Only separators (whitespace, hyphens, dots, and similar punctuation — noise a
fax/OCR read or a keyed-in value can carry) are stripped. Letters are never
coerced (no ``O`` -> ``0`` guessing): a mis-corrected licence would attach a
document to the wrong driver, which is worse than no licence at all.

Security: the licence is PII — never log it or put it on a queue message; use
``driver_key`` instead.
"""

from __future__ import annotations

import re
from typing import Final

CANONICAL_LENGTH: Final = 8
_VALID_LENGTHS: Final = frozenset({7, 8})
# Separators a physical-card read or keyed-in value may carry; anything else
# (letters, symbols) makes the value invalid rather than being silently dropped.
_SEPARATORS = re.compile(r"[\s\-._/]+")
_DIGITS = re.compile(r"\d+")


def normalize_licence(raw: str | None) -> str | None:
    """Return the canonical 8-digit licence number, or None if ``raw`` is invalid.

    Invalid means: empty, containing anything other than digits and separators,
    or not 7 or 8 digits long once separators are removed.
    """
    if raw is None:
        return None
    digits = _SEPARATORS.sub("", raw)
    if not _DIGITS.fullmatch(digits) or len(digits) not in _VALID_LENGTHS:
        return None
    return digits.zfill(CANONICAL_LENGTH)
