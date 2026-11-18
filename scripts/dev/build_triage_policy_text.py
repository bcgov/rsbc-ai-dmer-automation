"""Convert a Triage Sort Procedures PDF to the text the triage LLM reads, and
verify it -- the same build() the workflow orchestrator runs when a new PDF is
uploaded, so a PDF can be checked before it is published.

Run (from the repository root, with dmer_common[triage] installed):

    python scripts/dev/build_triage_policy_text.py <procedures.pdf> [--out <file.txt>]

Prints the PDF's SHA-256 (the text's name in the triage-policy container) and
the procedures' "Updated" date (the version that must be registered in
rules_version). Exit code 0: converted and verified; 1: it does not verify --
publishing that PDF would stop the triage LLM for every document.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from dmer_common.triage import PolicyTextError, build, updated_date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--out", type=Path, help="write the text here")
    args = parser.parse_args()

    pdf = args.pdf.read_bytes()
    print(f"PDF sha256: {hashlib.sha256(pdf).hexdigest()}")
    try:
        text = build(pdf)
    except PolicyTextError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    updated = updated_date(text)
    print(f"Verified: {text.count('=== Page ')} pages, {len(text):,} characters")
    print(f"Procedures version (rules_version): {updated or 'NOT READABLE'}")
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"Written to {args.out}")
    return 0 if updated else 1


if __name__ == "__main__":
    sys.exit(main())
