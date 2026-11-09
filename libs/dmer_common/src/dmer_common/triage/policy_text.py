"""The Triage Sort Procedures PDF as the text the triage LLM reads.

:func:`convert` turns the PDF into text: pages without a table (intro,
acronyms, table of contents) are kept as text, and every rule table becomes
one block per condition, with the two driver-class columns listed separately
(criteria -> action, in PDF order), so a threshold can never drift into the
other column::

    === Page 3 | Guide Sec. 4 Chapter 3: Cardiovascular Diseases and Disorders ===
    - Condition: Aortic Stenosis/Regurgitation; ...
      Class 5-8:
        * No concerns -> IN*
      High Class 1-4 / Class 5 with R18, 19, 20:
        * No concerns -> CP

The columns are listed separately because their rows do not line up in the
PDF (one High Class rule can sit beside two Class 5-8 rules). Text on the page
outside the table (notes, footnotes) is kept under "Notes on this page".

:func:`verify` checks a text against the PDF independently of the conversion:
every word on each page is taken from the page itself, by position, and
compared with that page's section of the text -- the words (nothing lost,
nothing added), the column each word is in, and the action codes per class
column. :func:`build` converts and verifies; a text that fails is never
returned.

pdfplumber (MIT) is imported only here, so the rest of the package works
without the ``triage`` extra installed.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from datetime import UTC, date, datetime

# Repeated on every page (footer); the legend is written once at the top
# instead. Matched as phrases: pdfplumber can put several on one line.
_BOILERPLATE = re.compile(
    r"P\s*a\s*g\s*e\s*\|\s*\d+"
    r"|Back to Table of Contents"
    r"|\* If initial DL application : Intake will send FIT letter requiring conditions"
    r"|Updated \d{1,2}\s*[A-Za-z]{3,9}\s*\d{4}"
)
LEGEND = (
    "* (asterisk after an action, e.g. IN*) = If initial DL application: "
    "Intake will send FIT letter requiring conditions."
)
HIGH = "High Class 1-4 / Class 5 with R18, 19, 20"
_ACTION_IN = re.compile(r"\b(CP|IN|PR|PU|TCM|CR)\b")
_ACTION = re.compile(r"^(CP|IN|PR|PU|TCM|CR)(\*?)$")
_UPDATED = re.compile(r"Updated\s+(\d{1,2}\s*[A-Za-z]{3,9}\s*\d{4})")
_RULE_TABLE_COLUMNS = (
    5  # condition | Class 5-8 criteria | action | High Class criteria | action
)


class PolicyTextError(ValueError):
    """The PDF can't be converted, or its text doesn't match it -- never use
    such a text. Names pages and counts only, never policy content."""


def _open(pdf: bytes):
    import pdfplumber  # the ``triage`` extra

    # Any parse failure means the same to the caller: not a usable PDF.
    try:
        return pdfplumber.open(io.BytesIO(pdf))
    except Exception as exc:  # noqa: BLE001
        raise PolicyTextError(f"Not a readable PDF: {type(exc).__name__}") from None


def _clean(text: str | None) -> str:
    text = (text or "").replace("�", "'")
    return " ".join(text.split())


def _lines(text: str | None) -> list[str]:
    """Cleaned, non-empty lines of *text* with the page boilerplate removed."""
    lines = []
    for line in (text or "").splitlines():
        line = _clean(_BOILERPLATE.sub(" ", line))
        if line:
            lines.append(line)
    return lines


def _rule_tables(page) -> list:
    """The page's rule tables. pdfplumber also reports small boxes drawn inside
    a rule table's cells as tables of their own; they are cell content."""
    tables = []
    for table in page.find_tables():
        rows = table.extract()
        if rows and len(rows[0]) == _RULE_TABLE_COLUMNS:
            tables.append(table)
    return tables


def _inside(obj: dict, bbox: tuple) -> bool:
    x0, top, x1, bottom = bbox
    cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
    return x0 <= cx <= x1 and top <= cy <= bottom


def _text_outside(page, bboxes: list[tuple]) -> list[str]:
    rest = page.filter(lambda obj: not any(_inside(obj, b) for b in bboxes))
    return _lines(rest.extract_text())


def _table(table) -> tuple[str, list[dict]]:
    """(chapter title from the header rows, [{condition, class58, high}])."""
    rows = [(list(r) + [None] * 5)[:5] for r in table.extract()]
    # Data starts at the first row with an action code in a class column.
    first = next(
        (
            i
            for i, r in enumerate(rows)
            if _ACTION_IN.search(_clean(r[2])) or _ACTION_IN.search(_clean(r[4]))
        ),
        len(rows),
    )
    header = " ".join(_clean(r[0]) for r in rows[:first] if r[0])
    chapter = _clean(re.sub(r"Class 5-8.*$", "", header))
    rules: list[dict] = []
    for r in rows[first:]:
        if _clean(r[0]):
            rules.append({"condition": _clean(r[0]), "class58": [], "high": []})
        elif not rules:
            rules.append(
                {
                    "condition": "(continued from the previous page)",
                    "class58": [],
                    "high": [],
                }
            )
        for comment, action, key in ((r[1], r[2], "class58"), (r[3], r[4], "high")):
            c, a = _clean(comment), _clean(action)
            if c or a:
                rules[-1][key].append(f"{c} -> {a}" if c and a else (c or a))
    return chapter, rules


def _updated(pdf_doc) -> str:
    """The "Updated <date>" footer of the first page, e.g. "12Jun2026"."""
    match = _UPDATED.search(pdf_doc.pages[0].extract_text() or "")
    return match[1].replace(" ", "") if match else "unknown date"


def updated_date(text: str) -> date | None:
    """The procedures' "Updated" date from a converted text's first line
    (``(updated 12Jun2026)``), or None when it can't be read."""
    match = re.search(
        r"\(updated (\d{1,2}[A-Za-z]{3,9}\d{4})\)", text.split("\n", 1)[0]
    )
    if not match:
        return None
    for fmt in ("%d%b%Y", "%d%B%Y"):
        try:
            return datetime.strptime(match[1], fmt).replace(tzinfo=UTC).date()
        except ValueError:
            continue
    return None


def convert(pdf: bytes) -> str:
    """The procedures PDF as text -- see the module docstring for the layout."""
    with _open(pdf) as doc:
        parts = [
            f"TRIAGE SORT PROCEDURES FOR DMERS (updated {_updated(doc)}) - converted from the PDF.",
            (
                "Each condition lists its Class 5-8 rules and its "
                f"{HIGH} rules separately, as 'criteria -> action', in the PDF's order."
            ),
            "Page numbers are the PDF's.",
            LEGEND,
            "",
        ]
        chapter = ""
        for number, page in enumerate(doc.pages, start=1):
            # Free each page once read: pdfplumber otherwise keeps every
            # page's objects (build() of the 42-page PDF peaked at ~180 MB;
            # ~25 MB with pages closed -- workers have 512 MB).
            try:
                tables = _rule_tables(page)
                if not tables:
                    parts.append(f"=== Page {number} ===")
                    parts += _lines(page.extract_text())
                    parts.append("")
                    continue
                for table in tables:
                    title, rules = _table(table)
                    chapter = title or chapter
                    parts.append(f"=== Page {number} | {chapter} ===")
                    for rule in rules:
                        parts.append(f"- Condition: {rule['condition']}")
                        parts.append("  Class 5-8:")
                        parts += [f"    * {x}" for x in rule["class58"]] or [
                            "    * (none listed)"
                        ]
                        parts.append(f"  {HIGH}:")
                        parts += [f"    * {x}" for x in rule["high"]] or [
                            "    * (none listed)"
                        ]
                notes = _text_outside(page, [t.bbox for t in tables])
                if notes:
                    parts.append("Notes on this page:")
                    parts += [f"  {n}" for n in notes]
                parts.append("")
            finally:
                page.close()
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Verification -- independent of the conversion above
# ---------------------------------------------------------------------------


def _words(text: str) -> list[str]:
    """Comparable tokens: lowercase, punctuation-insensitive, '*' its own token."""
    for old, new in (
        ("�", "'"),
        ("’", "'"),
        ("‘", "'"),
        ("“", '"'),
        ("”", '"'),
        ("*", " * "),
    ):
        text = text.replace(old, new)
    return re.findall(r"[a-z0-9%<>=+*]+", text.lower())


def _empty_page() -> dict[str, list[str]]:
    return {
        "all": [],
        "condition": [],
        "class58": [],
        "high": [],
        "class58_actions": [],
        "high_actions": [],
        "notes": [],
        "header": [],
    }


def _text_pages(text: str) -> dict[int, dict[str, list[str]]]:
    """Per page of a converted text: all words, and words by column."""
    pages: dict[int, dict[str, list[str]]] = {}
    current = column = None
    for line in text.splitlines():
        match = re.match(r"=== Page (\d+)(?: \| (.*))? ===$", line)
        if match:
            current = pages.setdefault(int(match[1]), _empty_page())
            column = None
            continue
        if current is None:
            continue
        s = line.strip()
        if s.startswith("- Condition:"):
            body = s.removeprefix("- Condition:").strip()
            if body != "(continued from the previous page)":
                current["condition"] += _words(body)
                current["all"] += _words(body)
            column = None
        elif s == "Class 5-8:":
            column = "class58"
        elif s == f"{HIGH}:":
            column = "high"
        elif s == "Notes on this page:":
            column = "notes"
        elif s.startswith("* ") and column in ("class58", "high"):
            body = s[2:]
            if body == "(none listed)":
                continue
            action = body.rpartition(" -> ")[2] if " -> " in body else body
            if _ACTION.match(action.strip()):
                current[f"{column}_actions"].append(action.strip())
            body = body.replace(" -> ", " ")  # the converter's separator, not PDF text
            current[column] += _words(body)
            current["all"] += _words(body)
        elif s:
            current["notes"] += _words(s)
            current["all"] += _words(s)
    return pages


def _page_words(page) -> list[dict]:
    """The page's words without the boilerplate. Words are grouped into lines
    by position; a boilerplate phrase is matched on the line's text and every
    word it covers is dropped."""
    lines: dict[int, list[dict]] = {}
    for word in page.extract_words():
        lines.setdefault(round(word["top"]), []).append(word)
    keep = []
    for words in lines.values():
        words.sort(key=lambda w: w["x0"])
        spans, pos = [], 0
        for word in words:
            spans.append((pos, pos + len(word["text"])))
            pos += len(word["text"]) + 1
        line = " ".join(w["text"] for w in words)
        cut = [m.span() for m in _BOILERPLATE.finditer(line)]
        keep += [
            w
            for w, (a, b) in zip(words, spans, strict=True)
            if not any(a < end and start < b for start, end in cut)
        ]
    return keep


def _column(x: float, edges: list[float]) -> int:
    """Which of the rule table's five columns *x* is in, by their left edges."""
    col = 0
    for i, edge in enumerate(edges):
        if x >= edge - 0.5:
            col = i
    return min(col, _RULE_TABLE_COLUMNS - 1)


def _pdf_pages(doc) -> dict[int, dict[str, list[str]]]:
    """Per PDF page: all words, and words by the column they sit in."""
    pages = {}
    for number, page in enumerate(doc.pages, start=1):
        try:
            words = _page_words(page)
            res = _empty_page()
            tables = _rule_tables(page)
            if not tables:
                for word in words:
                    res["all"] += _words(word["text"])
                pages[number] = res
                continue
            table = tables[0]
            rows = table.extract()
            first = next(
                (
                    i
                    for i, r in enumerate(rows)
                    if any(
                        _ACTION.match(
                            (r[j] or "").split()[-1] if (r[j] or "").strip() else ""
                        )
                        for j in (2, 4)
                    )
                ),
                None,
            )
            header_bottom = (
                table.rows[first].bbox[1] if first is not None else table.bbox[3]
            )
            # The five columns' left edges, from the first data row's cells.
            data_row = table.rows[first] if first is not None else table.rows[-1]
            edges = [cell[0] for cell in data_row.cells if cell]
            if len(edges) < _RULE_TABLE_COLUMNS:
                edges = sorted({round(c[0], 1) for c in table.cells if c})[
                    :_RULE_TABLE_COLUMNS
                ]

            for word in words:
                tokens = _words(word["text"])
                res["all"] += tokens
                if not _inside(word, table.bbox):
                    res["notes"] += tokens
                    continue
                if (word["top"] + word["bottom"]) / 2 < header_bottom:
                    res["header"] += tokens
                    continue
                col = _column((word["x0"] + word["x1"]) / 2, edges)
                key = ("condition", "class58", "class58", "high", "high")[col]
                res[key] += tokens
                if col in (2, 4) and _ACTION.match(word["text"].strip()):
                    res[f"{key}_actions"].append(word["text"].strip())
            pages[number] = res
        finally:
            page.close()
    return pages


def _diff(a: list[str], b: list[str]) -> tuple[int, int]:
    ca, cb = Counter(a), Counter(b)
    return sum((ca - cb).values()), sum((cb - ca).values())


def verify(pdf: bytes, text: str) -> list[str]:
    """Every difference between *text* and the PDF, per page: words lost or
    added, words in the wrong column, and action codes that differ per class
    column. Empty when the text matches. Names pages, columns and counts only."""
    with _open(pdf) as doc:
        pdf_pages = _pdf_pages(doc)
    text_pages = _text_pages(text)
    problems = []
    for number, p in sorted(pdf_pages.items()):
        t = text_pages.get(number)
        if t is None:
            problems.append(f"page {number}: missing from the text")
            continue
        page_words = list(p["all"])
        for word in p[
            "header"
        ]:  # table header rows: the text has the chapter title instead
            page_words.remove(word)
        lost, added = _diff(page_words, t["all"])
        if lost or added:
            problems.append(f"page {number}: {lost} word(s) lost, {added} added")
        if p["condition"] or p["class58"] or p["high"]:
            for col in ("condition", "class58", "high"):
                lost, added = _diff(p[col], t[col])
                if lost or added:
                    problems.append(f"page {number}: column {col} differs")
            for col in ("class58", "high"):
                if Counter(p[f"{col}_actions"]) != Counter(t[f"{col}_actions"]):
                    problems.append(f"page {number}: actions in column {col} differ")
    for number in sorted(set(text_pages) - set(pdf_pages)):
        problems.append(f"page {number}: in the text but not in the PDF")
    return problems


def build(pdf: bytes) -> str:
    """The PDF's text, converted and verified. Raises :class:`PolicyTextError`
    when verification finds any difference -- a text that doesn't match its
    PDF is never returned."""
    text = convert(pdf)
    problems = verify(pdf, text)
    if problems:
        raise PolicyTextError(
            f"Converted text does not match the PDF on {len(problems)} check(s): "
            + "; ".join(problems[:5])
        )
    return text
