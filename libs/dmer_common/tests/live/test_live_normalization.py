"""Live normalization tests: the real Azure OpenAI deployment, end to end.

These call the model, so they cost money, take minutes, and can vary from run
to run (model variance, and the deployment's output masking -- see
``dmer_common.normalization.unmask``). They are skipped unless
``DMER_LIVE_TESTS=1`` and the usual Azure OpenAI settings
(``dmer_common.config.openai_settings``) are set::

    DMER_LIVE_TESTS=1 pytest tests/live -n 6        # with pytest-xdist
    DMER_LIVE_TESTS=1 pytest tests/live -k checklist

Inputs are synthetic, DI-shaped documents: every field of the DI custom model
(``di_fields.json``) blank, with the case's Section D text and fields set.

- ``section_d_cases.json``: 261 condition terms from the Triage Sort
  Procedures and the field reference's keywords, each with the field(s) it
  must set. Run one per document, and four unrelated ones per document.
- ``checklist_cases.json``: the normalization checklist -- multiple
  conditions, checkbox plus Section D, cut-off and misread text, handwritten
  fields, alcohol seizure, compliance, no other conditions, DMER type,
  licence class and dates.
"""

from __future__ import annotations

import json
import os
from itertools import zip_longest
from pathlib import Path

import pytest

_HERE = Path(__file__).parent
_ENABLED = os.getenv("DMER_LIVE_TESTS") == "1"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not _ENABLED,
        reason="live model tests (set DMER_LIVE_TESTS=1 and Azure OpenAI settings)",
    ),
]


def _load(name: str) -> list:
    return json.loads((_HERE / name).read_text(encoding="utf-8"))


_DI_FIELDS = _load("di_fields.json")["fields"]
SECTION_D_CASES = _load("section_d_cases.json")
CHECKLIST_CASES = _load("checklist_cases.json")
_PER_DOCUMENT = 4


@pytest.fixture(scope="module")
def client():
    from dmer_common.config import openai_settings
    from dmer_common.openai_client import OpenAIClient

    return OpenAIClient(settings=openai_settings())


def di_document(section_d: str, fields: dict | None = None) -> dict:
    """What DI sends for a blank form, with *section_d* and *fields* filled in."""
    doc = {
        f["fieldKey"]: ("unselected" if f["fieldType"] == "selectionMark" else "")
        for f in _DI_FIELDS
    }
    doc.update({"current_license_class": "5", "medical_examination_date": "2026-08-15"})
    doc["details_of_condition"] = section_d
    doc.update(fields or {})
    return doc


def mismatches(dmer: dict, expected) -> dict:
    """Fields that don't hold the expected value, with what they hold.

    *expected* is a field name (must be true), ``{"any_of": [...]}`` (one must
    be true), or ``{field: value}`` -- ``"*"`` means any non-blank text.
    """
    if isinstance(expected, str):
        return {} if dmer.get(expected) is True else {expected: dmer.get(expected)}
    if set(expected) == {"any_of"}:
        names = expected["any_of"]
        if any(dmer.get(n) is True for n in names):
            return {}
        return {n: dmer.get(n) for n in names}
    bad = {}
    for field, want in expected.items():
        got = dmer.get(field)
        if want == "*":
            ok = isinstance(got, str) and bool(got.strip())
        elif isinstance(want, bool) or want is None:
            ok = got is want
        elif isinstance(want, (int, float)) and not isinstance(got, bool):
            ok = got is not None and float(got) == float(want)
        else:
            ok = got == want
        if not ok:
            bad[field] = got
    return bad


def _mixed_documents() -> list[list[dict]]:
    """Unrelated chapters, four to a document (round-robin across chapters)."""
    by_chapter: dict[str, list[dict]] = {}
    for case in SECTION_D_CASES:
        by_chapter.setdefault(case["chapter"], []).append(case)
    order = [c for group in zip_longest(*by_chapter.values()) for c in group if c]
    docs, current, chapters = [], [], set()
    for case in order:
        if len(current) == _PER_DOCUMENT or case["chapter"] in chapters:
            docs.append(current)
            current, chapters = [], set()
        current.append(case)
        chapters.add(case["chapter"])
    if current:
        docs.append(current)
    return docs


@pytest.mark.parametrize(
    "case", SECTION_D_CASES, ids=[c["section_d"][:40] for c in SECTION_D_CASES]
)
def test_section_d_term_alone(client, case):
    from dmer_common.normalization import normalize_document

    dmer = normalize_document(client, di_document(case["section_d"]))
    assert mismatches(dmer, case["expected"]) == {}


@pytest.mark.parametrize(
    "cases",
    _mixed_documents(),
    ids=[" ".join(c["section_d"][:15] for c in d) for d in _mixed_documents()],
)
def test_section_d_terms_together(client, cases):
    from dmer_common.normalization import normalize_document

    dmer = normalize_document(
        client, di_document(" ".join(c["section_d"] for c in cases))
    )
    missed = {
        c["section_d"]: bad for c in cases if (bad := mismatches(dmer, c["expected"]))
    }
    assert missed == {}


@pytest.mark.parametrize(
    "case",
    CHECKLIST_CASES,
    ids=[f'{c["group"]}: {c["case"]}' for c in CHECKLIST_CASES],
)
def test_checklist(client, case):
    from dmer_common.normalization import normalize_document

    dmer = normalize_document(client, di_document(case["section_d"], case["fields"]))
    assert mismatches(dmer, case["expected"]) == {}
