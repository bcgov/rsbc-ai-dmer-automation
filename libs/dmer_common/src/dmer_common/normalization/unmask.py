"""Restore words the Azure OpenAI deployment masks in model output.

The deployment replaces some words in a completion with asterisks of the
same length ("Permanent pacemaker." comes back as "Permanent *********."),
in values, in evidence quotes, and sometimes in field names. The model read
the real words -- they are in the input we sent it -- so a masked span can be
put back by matching it against that input:

- A masked value is matched word by word: each masked run becomes "any n
  non-space characters", with up to :data:`CONTEXT_WORDS` neighbouring words
  on each side as literal context. The widest context that matches anything
  is used, and only a single distinct match is restored -- no match, or more
  than one, leaves the mask in place (the evidence checks then flag it).
- A masked field name is matched against the field names the call may
  return. When several fit, the one whose condition is named in the input
  wins ("general.******" with "Hernia." in the input is general.hernia);
  otherwise it stays masked.

Only text from the call's own input is ever used, so nothing the model was
not already given ends up in the output. Each masked value is logged before
and after restoration (normalization input carries no PII), so a wrong
restoration -- or a model that answered a masked input with a made-up value
-- can be traced in the logs.
"""

import re
from collections.abc import Collection, Iterable, Mapping

from ..telemetry import get_logger

_log = get_logger(__name__)

# Two or more: the deployment masks short spans too ("**/16**" for "20/16-2").
# A single "*" is left alone -- it is common in real text ("CP*").
MASK_RE = re.compile(r"\*{2,}")
# Neighbouring words used as literal context around a masked run.
CONTEXT_WORDS = 3
# Quote marks and "field_name:" labels separate an evidence string into parts
# that come from different places, so context never crosses them.
_SEGMENT_SPLIT_RE = re.compile(r'(["“”]|(?:^|(?<=\s))[\w.]+:(?=\s|$))')


# Cap on logged text: an Application Insights trace holds about 32 KB.
MAX_LOGGED_CHARS = 30_000


def has_mask(text: str) -> bool:
    return bool(MASK_RE.search(text))


def source_strings(source: object) -> list[str]:
    """Every non-blank string in *source* (nested dicts/lists included)."""
    if isinstance(source, str):
        return [source] if source.strip() else []
    if isinstance(source, dict):
        return [s for value in source.values() for s in source_strings(value)]
    if isinstance(source, (list, tuple)):
        return [s for value in source for s in source_strings(value)]
    return []


def _token_pattern(token: str, capture: int | None = None) -> str:
    """Regex for one whitespace-free token; masked runs match any non-space
    characters of the same length. The *capture*-th run is a group."""
    out, pos = [], 0
    for index, match in enumerate(MASK_RE.finditer(token)):
        out.append(re.escape(token[pos : match.start()]))
        width = len(match.group())
        out.append(rf"(\S{{{width}}})" if index == capture else rf"\S{{{width}}}")
        pos = match.end()
    out.append(re.escape(token[pos:]))
    return "".join(out)


def _match_run(
    words: list[str], at: int, run: int, texts: list[str]
) -> tuple[str | None, str, int]:
    """Find the source text for the *run*-th masked run of ``words[at]``.

    Returns (replacement or None, outcome, context words used)."""
    tried = set()
    for k in range(CONTEXT_WORDS, -1, -1):
        left, right = words[max(0, at - k) : at], words[at + 1 : at + 1 + k]
        if (len(left), len(right)) in tried:
            continue
        tried.add((len(left), len(right)))
        pattern = r"\s+".join(
            [
                *(_token_pattern(w) for w in left),
                _token_pattern(words[at], run),
                *(_token_pattern(w) for w in right),
            ]
        )
        regex = re.compile(rf"(?<!\w){pattern}(?!\w)", re.IGNORECASE)
        found = {}
        for text in texts:
            for match in regex.finditer(text):
                found.setdefault(match.group(1).casefold(), match.group(1))
        if len(found) == 1:
            return next(iter(found.values())), "restored", max(len(left), len(right))
        if len(found) > 1:
            return None, "ambiguous", max(len(left), len(right))
    return None, "no_match", 0


def _restore_segment(segment: str, texts: list[str], step: str, field: str) -> str:
    parts = re.split(r"(\s+)", segment)
    word_slots = [i for i in range(0, len(parts), 2) if parts[i]]
    for position, slot in enumerate(word_slots):
        run = 0
        while True:
            runs = list(MASK_RE.finditer(parts[slot]))
            if run >= len(runs):
                break
            words = [parts[i] for i in word_slots]
            replacement, outcome, context = _match_run(words, position, run, texts)
            if replacement is not None and has_mask(replacement):
                # The source itself has asterisks there (OCR read "*****"):
                # nothing to restore, and substituting it back would find the
                # same run forever (dev 2026-10-20: one normalization looped
                # for 27 minutes).
                replacement, outcome = None, "in_source"
            width = len(runs[run].group())
            log = _log.info if replacement is not None else _log.warning
            log(
                "masked model output",
                extra={
                    "step": step,
                    "field": field,
                    "location": "value",
                    "mask_length": width,
                    "context_words": context,
                    "outcome": outcome,
                },
            )
            if replacement is None:
                run += 1  # leave this run masked; try the next one
                continue
            span = runs[run]
            parts[slot] = (
                parts[slot][: span.start()] + replacement + parts[slot][span.end() :]
            )
    return "".join(parts)


def _restore_text(text: str, texts: list[str], step: str, field: str) -> str:
    if not has_mask(text) or any(text in source for source in texts):
        return text  # no mask, or the asterisks are really in the source
    pieces = _SEGMENT_SPLIT_RE.split(text)
    return "".join(
        piece if not has_mask(piece) else _restore_segment(piece, texts, step, field)
        for piece in pieces
    )


_KEY_SUFFIXES = ("_evidence", "_has_concerns", "_has_concern", ".has_concerns")


def _named_in(key: str, texts: list[str]) -> bool:
    """Is the condition *key* refers to named in *texts*?"""
    local = key
    for suffix in _KEY_SUFFIXES:
        local = local.removesuffix(suffix)
    words = local.rsplit(".", 1)[-1].replace("_", " ").strip()
    if not words:
        return False
    regex = re.compile(rf"(?<!\w){re.escape(words)}(?!\w)", re.IGNORECASE)
    return any(regex.search(text) for text in texts)


def _restore_key(
    key: str, known_keys: Collection[str], texts: list[str], step: str
) -> str:
    out, pos = [], 0
    for match in MASK_RE.finditer(key):
        out.append(re.escape(key[pos : match.start()]))
        out.append(f".{{{len(match.group())}}}")
        pos = match.end()
    out.append(re.escape(key[pos:]))
    regex = re.compile("".join(out))
    candidates = sorted(k for k in known_keys if regex.fullmatch(k))
    matched_by = "name"
    if len(candidates) > 1:
        named = [k for k in candidates if _named_in(k, texts)]
        if named:
            candidates, matched_by = named, "source_text"
    restored = candidates[0] if len(candidates) == 1 else None
    outcome = "restored" if restored else ("ambiguous" if candidates else "no_match")
    log = _log.info if restored else _log.warning
    log(
        "masked model output",
        extra={
            "step": step,
            # A restored name is a schema field name; an unresolved one is
            # shown masked -- neither carries document text.
            "field": restored or key,
            "location": "key",
            "mask_length": sum(len(m.group()) for m in MASK_RE.finditer(key)),
            "candidates": len(candidates),
            "matched_by": matched_by if restored else None,
            "outcome": outcome,
        },
    )
    return restored or key


def source_fields(source: object) -> dict[str, str]:
    """Field name -> non-blank string value of every field in *source* (a
    DMER dict, ``{"dmer": {...}}``, or a list holding them)."""
    if isinstance(source, (list, tuple)):
        merged: dict[str, str] = {}
        for item in source:
            merged.update(source_fields(item))
        return merged
    if not isinstance(source, dict):
        return {}
    if isinstance(source.get("dmer"), dict):
        return source_fields(source["dmer"])
    return {k: v for k, v in source.items() if isinstance(v, str) and v.strip()}


# "field.name:" labels inside an evidence string ('a.b: "quote"; c.d: true').
_EVIDENCE_LABEL_RE = re.compile(r"(?:^|(?<=[\s;]))([A-Za-z_][\w./]*):(?=\s|$)")


def evidence_labels(evidence: str) -> list[str]:
    """The ``field.name:`` labels an evidence string cites."""
    return _EVIDENCE_LABEL_RE.findall(evidence)


# Labels the deployment masked ("**********: true"): a word with a masked run.
_MASKED_LABEL_RE = re.compile(r'(?:^|(?<=[\s;]))([^\s;:"]*\*{2,}[^\s;:"]*):(?=\s|$)')


def masked_evidence_labels(evidence: str) -> list[str]:
    """The masked ``*****:`` labels an evidence string cites -- a field name
    the model saw (partly) as asterisks."""
    return _MASKED_LABEL_RE.findall(evidence)


def resolve_masked_label(label: str, source: Mapping[str, object]) -> str | None:
    """The one source field a masked evidence label can be -- same length,
    unmasked parts equal ("***********.other_psych_diagnosis" is
    psychiatric.other_psych_diagnosis), and a value on the form (ticked or
    filled in). None when no field, or more than one, fits."""
    out, pos = [], 0
    for match in MASK_RE.finditer(label):
        out.append(re.escape(label[pos : match.start()]))
        out.append(f".{{{len(match.group())}}}")
        pos = match.end()
    out.append(re.escape(label[pos:]))
    regex = re.compile("".join(out))
    candidates = [
        name
        for name, value in source.items()
        if regex.fullmatch(name)
        and (value is True or (isinstance(value, str) and value.strip()))
    ]
    return candidates[0] if len(candidates) == 1 else None


def _field_texts(name: str, value: str, fields: Mapping[str, str]) -> list[str]:
    """The input text a value most likely came from: the field's own input
    value, or -- for an evidence string -- the fields its labels name."""
    if name.endswith("_evidence"):
        labels = _EVIDENCE_LABEL_RE.findall(value)
        return [fields[label] for label in labels if label in fields]
    return [fields[name]] if name in fields else []


def restore_masked(
    response: object,
    texts: Iterable[str],
    known_keys: Collection[str] = (),
    *,
    step: str,
    field: str = "",
    fields: Mapping[str, str] | None = None,
) -> object:
    """Return *response* with masked keys and values restored where the
    call's input (*texts*) or its allowed field names (*known_keys*)
    identify exactly one original.

    With *fields* (the input's field -> value), a masked value is first
    matched against the same field's input value only -- a short value like
    ``"20***"`` with no words around it is ambiguous across the whole input
    but exact against its own field ("20/25"). Field names are restored
    first (rarely masked), so the field is known by then. Only if that
    leaves a mask does the whole-input search run.
    """
    texts = list(texts)
    fields = fields or {}
    if isinstance(response, dict):
        restored = {}
        for key, value in response.items():
            name = key
            if isinstance(key, str) and has_mask(key):
                name = _restore_key(key, known_keys, texts, step)
                if name != key and name in response:
                    name = key  # the real key is already there; keep both apart
            restored[name] = restore_masked(
                value, texts, known_keys, step=step, field=str(name), fields=fields
            )
        return restored
    if isinstance(response, list):
        return [
            restore_masked(
                item, texts, known_keys, step=step, field=field, fields=fields
            )
            for item in response
        ]
    if isinstance(response, str):
        masked = has_mask(response)
        own = _field_texts(field, response, fields) if masked else []
        restored = response
        if own:
            restored = _restore_text(restored, own, step, field)
        restored = _restore_text(restored, texts, step, field)
        if masked:
            _log.info(
                "masked model output values",
                extra={
                    "step": step,
                    "field": field,
                    "masked_value": response[:MAX_LOGGED_CHARS],
                    "restored_value": restored[:MAX_LOGGED_CHARS],
                    "still_masked": has_mask(restored),
                },
            )
        return restored
    return response
