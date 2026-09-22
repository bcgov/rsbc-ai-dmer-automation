# Azure Blob Storage

Source: architecture doc §3.2, §4, §9.1. Bicep modules:
`infrastructure/bicep/modules/storage/{storage-account,blob-containers}.bicep`.

## Containers

Four containers, one per pipeline artifact stage — this is the revised architecture's layout and
**replaces** the original architecture's seven-container layout (`raw`, `ocr`, `normalized`,
`rules`, `audit`, `failed`, `archive`). See [Alignment gaps](#alignment-gaps-vs-current-code).

| Container | Written by | Contents | Immutable? |
|---|---|---|---|
| `raw-dmer` | [Ingest](../stages/01-ingest.md) | Source PDFs, path `{yyyy}/{MM}/{document_guid}.pdf` | Yes — the evidentiary copy, never rewritten by a later stage. |
| `extracted-dmer` | [Extraction](../stages/02-extraction.md) | One combined JSON per document (`{document_id}.json`), with the custom-model result and handwriting result as **named sections within one object** | Overwritten on replay of the same document only. |
| `normalized-dmer` | [Activity: Normalize](../stages/04-activity-normalize.md) | One normalized JSON per document | Overwritten on replay. |
| `rules` | Rule authoring/publishing process (not a pipeline stage) | Versioned `rules.json` — see [Versioning](#versioning-rules) below | Versions immutable; `active` pointer mutable. |

## Versioning `rules`

`rules/active/rules.json` is what [Activity: Rule Engine](../stages/05-activity-rule-engine.md)
reads. `rules/versions/<version>/rules.json` holds every published version. A rule change is
tested against golden files and rolled back **by pointer swap**, not by redeploying the rule engine
(this pattern — `active/` vs `versions/<version>/` — comes from
`docs/architecture/repository-design.md` §13 item 6 and still applies under the revised
architecture; the architecture document's own `rules_version` table, in `../data-model.md`, is the
DB-side record of the same versioning).

## Path conventions

Every path builder is **deterministic** so a retry overwrites the same blob rather than creating a
second copy — this is load-bearing for the replay guards in every stage doc (e.g.
[Ingest](../stages/01-ingest.md#ingest-function--processing-steps) step 3). Namespace every
extraction/normalization blob by `document_id` (internal uuid), not `document_guid`.

## Data protection

- Soft delete and versioning enabled on all four containers (§9.1). The `rules` container's
  versioning additionally serves auditability of past decisions (a decision's `rules_version` FK
  points at a specific published version — see `../data-model.md`).
- Retention: 30–90 days for raw/extracted/normalized artifacts (question I-17, answered) —
  implement as a lifecycle management policy per container once this is finalized; not yet
  implemented anywhere in the Bicep modules.
- No public network access; private endpoint only, inside the platform VNet.

## Authentication / identity

Managed identity (`Storage Blob Data Contributor` for writers, `Storage Blob Data Reader` where
read-only suffices), scoped per container per service — e.g. the Extraction processor needs
Contributor on `raw-dmer` (read) and `extracted-dmer` (write), but no access to `normalized-dmer` or
`rules`.

## Implementation considerations for Claude Code

- `libs/dmer_common/src/dmer_common/storage/client.py` (`BlobClient`: `download`, `upload_json`,
  `upload_bytes`, `blob_url`, managed-identity `DefaultAzureCredential`) is reusable **as-is** across
  all four containers — it's already container-agnostic.
- `libs/dmer_common/src/dmer_common/storage/{containers.py,paths.py}` need to be **rewritten** for
  the four-container layout above — see [Alignment gaps](#alignment-gaps-vs-current-code). Keep the
  existing pattern (env-var-overridable container names via `os.getenv`, pure path-builder functions
  with no Azure SDK dependency so they're unit-testable) — only the concrete names/paths change.

## Alignment gaps vs. current code

`libs/dmer_common/src/dmer_common/storage/containers.py` currently defines
`EXTRACTED_DMER_CONTAINER` (default `extracted-dmer`) and `COMBINED_EXTRACTED_DMER_CONTAINER`
(default `combined-extracted-dmer`) — a two-container split for extraction sub-stages
(`top_level.json`, `ocr.json`, `handwritten.json` in `extracted-dmer`; `combined.json` in
`combined-extracted-dmer`), per `paths.py`. The revised architecture merges these into **one**
`extracted-dmer` container holding one combined JSON per document with named sections — see
[Extraction §Blob writes](../stages/02-extraction.md#blob-writes). There is also no `raw-dmer`,
`normalized-dmer`, or `rules` container defined in `dmer_common` yet, and
`docs/architecture/repository-design.md`'s seven-container list (`raw`, `ocr`, `normalized`,
`rules`, `audit`, `failed`, `archive`) is superseded by the four-container list above — `audit`,
`failed`, and `archive` have no equivalent in the revised architecture (dead-lettering + the
`processing_error` table cover what `failed`/`audit` were for; no `archive` container is mentioned).
`infrastructure/bicep/modules/storage/blob-containers.bicep` has no containers instantiated yet, so
there's no infra drift — only the `dmer_common` module and old docs need reconciling.
