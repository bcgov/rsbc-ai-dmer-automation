"""
DMER Field Validator – entry point.
====================================
Reads a DMER JSON file, runs it through the Azure OpenAI analysis
pipeline, and writes the validated result to ``results/<input_name>_validated.json``.
"""

import json
import os
import sys

from src.processing import (
    analyze_conditions,
    apply_updates,
    ensure_all_fields,
    flag_concerns,
    normalize_dates,
)


def main() -> None:
    dmer_path = sys.argv[1] if len(sys.argv) > 1 else "dmer.json"

    with open(dmer_path, "r") as f:
        dmer_data = json.load(f)

    print(f"Analyzing DMER JSON from: {dmer_path}\n")

    updates = analyze_conditions(dmer_data)

    # Show what the LLM returned
    print(f"\n--- LLM returned {len(updates.get('dmer', {}))} fields ---")
    print(json.dumps(updates, indent=2))

    updated_dmer = apply_updates(dmer_data, updates)

    # Backfill any missing fields with defaults so the output has every field
    updated_dmer = ensure_all_fields(updated_dmer)

    # Deterministically flag _has_concerns fields the LLM may have missed
    # updated_dmer = flag_concerns(updated_dmer)

    # Normalize all date fields to YYYY-MM-DD
    updated_dmer = normalize_dates(updated_dmer)

    os.makedirs("results", exist_ok=True)

    base_name = os.path.splitext(os.path.basename(dmer_path))[0]
    output_path = f"results/{base_name}_validated.json"

    with open(output_path, "w") as f:
        json.dump(updated_dmer, f, indent=2)

    print(f"\nSaved to {output_path}")


if __name__ == "__main__":
    main()
