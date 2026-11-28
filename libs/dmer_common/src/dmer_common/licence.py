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

**``00000000`` is not a licence.** Mercury files a DMER its own systems
couldn't map to a driver under a placeholder driver whose licence is
``00000000`` (:data:`PLACEHOLDER_LICENCE`, confirmed 2026-11-23). That value --
from Mercury or read off a page -- must never match a driver, so it normalizes
to None like any other invalid value: the DMER is treated as having no driver,
and the driver is found from the licence read on the page instead.

Security: the licence is PII — never log it or put it on a queue message; use
``driver_key`` instead.
"""

from __future__ import annotations

import re
from typing import Final

CANONICAL_LENGTH: Final = 8
# Mercury's placeholder driver's licence: the DMER isn't mapped to a driver.
PLACEHOLDER_LICENCE: Final = "00000000"
_VALID_LENGTHS: Final = frozenset({7, 8})
# Separators a physical-card read or keyed-in value may carry; anything else
# (letters, symbols) makes the value invalid rather than being silently dropped.
_SEPARATORS = re.compile(r"[\s\-._/]+")
_DIGITS = re.compile(r"\d+")


def normalize_licence(raw: str | None) -> str | None:
    """Return the canonical 8-digit licence number, or None if ``raw`` is invalid.

    Invalid means: empty, containing anything other than digits and separators,
    not 7 or 8 digits long once separators are removed, or Mercury's placeholder
    ``00000000`` (:data:`PLACEHOLDER_LICENCE`) once canonical.
    """
    if raw is None:
        return None
    digits = _SEPARATORS.sub("", raw)
    if not _DIGITS.fullmatch(digits) or len(digits) not in _VALID_LENGTHS:
        return None
    canonical = digits.zfill(CANONICAL_LENGTH)
    return None if canonical == PLACEHOLDER_LICENCE else canonical


def is_placeholder_licence(raw: str | None) -> bool:
    """True for Mercury's placeholder licence ``00000000``
    (:data:`PLACEHOLDER_LICENCE`; separators ignored): the DMER isn't mapped
    to a driver in Mercury."""
    return _SEPARATORS.sub("", raw or "") == PLACEHOLDER_LICENCE
