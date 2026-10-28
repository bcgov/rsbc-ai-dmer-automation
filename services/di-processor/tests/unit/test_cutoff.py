"""Unit tests for cut-off detection (has_header / has_signature / is_cutoff).

Behaviour specs (GIVEN/WHEN/THEN). The fixture is trimmed from a real
``rsbc-ocr-dmer-v9`` analyze result whose page 1 is cut off at the bottom; the
other cases use synthetic DI-shaped pages.
"""

from __future__ import annotations

import json
from pathlib import Path

from di_processor.extraction.cutoff import (
    HEADER_ANCHORS,
    SIGNATURE_ANCHORS,
    detect_cutoff,
    located_fields,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "top_level_raw_cutoff_bottom.json"

PAGE_H = 11.0


def _line(content: str, y_ratio: float) -> dict:
    """A DI line whose top edge sits at ``y_ratio`` of the page height."""
    y0 = y_ratio * PAGE_H
    y1 = y0 + 0.12
    return {"content": content, "polygon": [0.5, y0, 5.0, y0, 5.0, y1, 0.5, y1]}


def _page(*lines: dict) -> dict:
    return {"pageNumber": 1, "width": 8.5, "height": PAGE_H, "lines": list(lines)}


HEADER_LINES = (
    _line("03/06/2024 WED 16:29 FAX 000 000 0000", 0.0),
    _line("RoadSafetyBC", 0.043),
    _line("DRIVER'S MEDICAL EXAMINATION", 0.057),
    _line("AREA ABOVE FOR OFFICE USE", 0.107),
)
BODY_LINES = (
    _line("B. VISION SCREENING AND PHYSICAL FINDINGS AFFECTING DRIVING", 0.5),
)
SIGNATURE_LINES = (
    _line("EXAMINING PHYSICIAN'S OR NP'S NAME AND ADDRESS", 0.90),
    _line("Examination Date", 0.93),
    _line("Physician's or NP's Signature", 0.93),
    _line("TELEPHONE NO.", 0.95),
    _line(
        "PHYSICIAN OR NP: FAX TO 250-952-6888 OR MAIL TO RoadSafetyBC, "
        "P.O. BOX 9254, STN PROV GOVT, VICTORIA, BC, V8W 9J2",
        0.98,
    ),
)


def test_real_sample_cut_off_at_bottom():
    # GIVEN the real DI output of a DMER whose signature block was cut off
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    page = raw["pages"][0]
    fields = raw["documents"][0]["fields"]

    # WHEN cut-off detection runs
    flags = detect_cutoff(page, fields)

    # THEN the header is present, the signature band is not, and it is cut off
    assert flags.has_header is True
    assert flags.has_signature is False
    assert flags.is_cutoff is True
    assert set(flags.header_anchors) == set(HEADER_ANCHORS)
    assert flags.signature_anchors == []
    # the examiner-block fields exist but were not located (confidence only)
    assert flags.signature_fields == []


def test_intact_page_is_not_cut_off():
    # GIVEN a page with both the header and the signature block
    page = _page(*HEADER_LINES, *BODY_LINES, *SIGNATURE_LINES)

    flags = detect_cutoff(page, {})

    # THEN both bands are present and it is not cut off
    assert (flags.has_header, flags.has_signature, flags.is_cutoff) == (
        True,
        True,
        False,
    )
    assert set(flags.signature_anchors) == set(SIGNATURE_ANCHORS)


def test_header_cut_off():
    # GIVEN the top of the form is missing (only the fax banner survived)
    page = _page(HEADER_LINES[0], *BODY_LINES, *SIGNATURE_LINES)

    flags = detect_cutoff(page, {})

    # THEN the header is missing and it is cut off; the fax banner never counts
    assert flags.has_header is False
    assert flags.has_signature is True
    assert flags.is_cutoff is True
    assert flags.header_anchors == []


def test_both_bands_cut_off():
    # GIVEN only the body of the form survived
    flags = detect_cutoff(_page(*BODY_LINES), {})

    assert (flags.has_header, flags.has_signature, flags.is_cutoff) == (
        False,
        False,
        True,
    )


def test_single_surviving_anchor_is_not_enough():
    # GIVEN only one header anchor survived (the top was partially cut)
    page = _page(HEADER_LINES[3], *BODY_LINES, *SIGNATURE_LINES)

    # THEN one anchor is below the 2-anchor minimum: header treated as missing
    assert detect_cutoff(page, {}).has_header is False


def test_noisy_ocr_still_matches():
    # GIVEN fax-degraded OCR of the anchors (the kind DI returns on faxes)
    page = _page(
        _line("RoadSafetyB C", 0.04),
        _line("DRIVER'S MEDlCAL EXAMlNATION", 0.06),
        *BODY_LINES,
        _line("EXAMINING PHYSICIAN'S OR NP's NAME AND ADDRES", 0.90),
        _line("Physicians or NP's Signalure", 0.93),
    )

    flags = detect_cutoff(page, {})

    # THEN the fuzzy match still finds both bands
    assert flags.has_header is True
    assert flags.has_signature is True
    assert flags.is_cutoff is False


def test_anchor_outside_its_band_is_ignored():
    # GIVEN signature-block labels appearing mid-page (e.g. a stapled page 2
    # rendered into page 1, or similar text in the body)
    page = _page(
        *HEADER_LINES,
        _line("Examination Date", 0.30),
        _line("TELEPHONE NO.", 0.35),
    )

    # THEN they do not count as the bottom band
    flags = detect_cutoff(page, {})
    assert flags.has_signature is False
    assert flags.is_cutoff is True


def test_located_signature_field_counts_without_anchors():
    # GIVEN the anchors were unreadable but the model located the exam date
    fields = {
        "medical_examination_date": {
            "type": "string",
            "valueString": "2024-03-06",
            "boundingRegions": [{"pageNumber": 1, "polygon": [1, 10, 2, 10]}],
            "confidence": 0.9,
        }
    }
    flags = detect_cutoff(_page(*HEADER_LINES, *BODY_LINES), fields)

    # THEN the signature band is considered present
    assert flags.has_signature is True
    assert flags.signature_fields == ["medical_examination_date"]
    assert flags.is_cutoff is False


def test_confidence_only_field_is_not_located():
    # GIVEN absent fields (DI still reports a high confidence that they're empty)
    fields = {
        "doctor_signature": {"type": "signature"},
        "medical_examination_date": {"type": "string", "confidence": 0.981},
        "physician_or_np_fax_present": {"type": "string", "confidence": 0.934},
    }
    # THEN none of them counts as located
    assert located_fields(fields, tuple(fields)) == []


def test_signed_signature_field_is_located():
    fields = {"doctor_signature": {"type": "signature", "valueSignature": "signed"}}
    assert located_fields(fields, ("doctor_signature",)) == ["doctor_signature"]


def test_no_layout_is_undeterminable():
    # GIVEN no page / no lines WHEN detecting THEN every flag is None, not False
    for page in (None, {}, _page(), {"height": 0, "lines": HEADER_LINES}):
        flags = detect_cutoff(page, {})
        assert (flags.has_header, flags.has_signature, flags.is_cutoff) == (
            None,
            None,
            None,
        )
