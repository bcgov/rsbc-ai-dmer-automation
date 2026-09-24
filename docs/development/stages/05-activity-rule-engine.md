# Activity: Rule Engine

Source: architecture doc §4.3.3. Part of [Document Orchestration](03-document-orchestration.md).
Data model: `../data-model.md`.

## Purpose

Loads the active `rules.json`, applies it to the normalized document, and receives a list of
candidate outcomes. A validation step then selects the highest-priority outcome.

## Why this is a library call, not a service

The rule engine (GoRules/Zen) is a **library**, not a service — it needs no container and no
network hop. It runs in-process inside the Durable activity. This is a change from the original
architecture, where `rule-engine` was its own HTTP-triggered Azure Function invoked by the
orchestrator — see [Alignment gaps](#alignment-gaps-vs-current-code).

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable activity function, running GoRules/Zen in-process, called from the Document Orchestrator |
| Reads | `normalized-dmer` blob; the active `rules.json` from the `rules` blob container |
| Writes | `rule_evaluation`, `dmer_document`, `dmer_stage_run` |

## Processing steps

1. Load the active `rules.json` from the `rules` container (see `../services/azure-blob-storage.md`
   for the `rules/active/` vs. `rules/versions/<version>/` layout).
2. Evaluate the normalized document against it, producing a list of candidate outcomes.
3. Select the highest-priority outcome per the validation step.
4. Persist **all** candidate outcomes, not just the winner, to `rule_evaluation`.
5. Increment the driver's `completed_document_count` (the `driver_evaluation` row was created or
   attached earlier in the same orchestration by
   [Resolve Driver](03-document-orchestration.md#activity-resolve-driver)).
6. Signal the driver by publishing to `driver-decision` (this happens at the orchestrator level, at
   the end of the whole document orchestration — see
   [Document Orchestration](03-document-orchestration.md#orchestration-flow)).

### Persist all candidates, not just the winner

When Intake disputes a decision months later, the question is always *which rule fired and which
version was active* — only the complete record answers it. Storing just the selected outcome makes
that question unanswerable after the fact.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `rule_evaluation` | `INSERT` (one row **per evaluation**, not per document — a re-run creates a new row) | `document_id`, `rules_version`, `all_outcomes` (jsonb — every candidate with its inputs), `selected_outcome_code`, `selected_reason`, `priority_rank`, `evaluated_at`. |
| `rules_version` | `INSERT` (only when a new `rules.json` is published) | `version`, `blob_url`, `checksum`, `activated_at`, `activated_by`. |
| `dmer_document` | `UPDATE` | `pipeline_status = RULES_APPLIED`, then `AWAITING_DRIVER_COMPLETION` once the driver is signalled; `current_stage = DECISION`, `updated_at`. |
| `driver_evaluation` | `UPDATE` | `completed_document_count` incremented — this counter and `expected_document_count` are what make the wait observable. |
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = RULES`, `status`, `attempt_no`, timings, `rules_version`. |

## Precedence: document_priority vs. rule engine

Question I-3 (answered): **the rule engine decides PR/PU/PCM/CR entirely from medical content.**
`dmer_document.document_priority` (from Mercury) does not override the outcome selection.

## Failure handling

A rules.json parse/validation failure at load time is poison for the whole batch of documents
currently in flight, not just one document — treat a corrupt or missing active `rules.json` as a
circuit-breaker-worthy condition (alert immediately; do not let it silently fall back to a stale
cached version without recording that it did). Per-document evaluation failures (a normalized field
missing that a rule expects) follow the standard transient/poison split; a missing required field is
poison, not transient.

## Interaction with upstream/downstream

Reads Normalize's output within the same orchestration instance. Its completion is what the
Document Orchestrator publishes to `driver-decision` for — see
[Document Orchestration](03-document-orchestration.md).

## Configuration / environment variables

| Variable | Purpose |
|---|---|
| `RULES_CONTAINER` | Default `rules`. |
| `RULES_ACTIVE_PATH` | Default `rules/active/rules.json`. |
| `RULE_ENGINE_LIBRARY` | GoRules/Zen binding — confirm Python package name during Phase 3 build (not pinned in any `requirements.txt` yet). |

## Idempotency requirements

Covered by orchestrator replay semantics, same as Normalize. The one caveat: `completed_document_count`
must only be incremented **once per document**, even if the activity is retried within an attempt —
increment via a `driver_evaluation_document` join/marker or an idempotent `UPSERT` keyed on
`(driver_evaluation_id, document_id)`, not a bare `UPDATE ... SET completed_document_count = completed_document_count + 1`
that would double-count on activity retry before the orchestrator's own dedup kicks in.

## Logging / auditing

Log `rules_version` and `selected_outcome_code` on every evaluation (not the full `all_outcomes`
payload at INFO level — it can carry section-level content close to clinical data; keep it in the
DB row, not the log stream).

## Implementation considerations for Claude Code

- No GoRules/Zen dependency is pinned in `services/rule-engine/requirements.txt` yet (placeholder
  comment only) — confirm the Python binding package name before starting Phase 3.
- `services/rule-engine/rules/README.md` already documents the intended local sample `rules.json`
  convention (mirrors `rules/active/rules.json` in Blob Storage) — keep using it for local dev/test
  fixtures regardless of where the evaluation code itself ends up living.
- This activity's code should live alongside the Document Orchestrator — see
  `03-document-orchestration.md#implementation-considerations-for-claude-code` for the recommended
  module boundary (library code in `libs/`, thin activity trigger in `workflow-orchestrator`).

## Alignment gaps vs. current code

`docs/services/rule-engine.md` and `services/rule-engine/` (placeholder Azure Functions folder,
HTTP-triggered) describe the rule engine as an independently deployable Function App invoked by the
orchestrator over HTTP. Under the revised architecture, rule evaluation is an **in-process library
call inside a Durable activity**, not a separate compute resource or network hop.
`services/rule-engine/` has no working evaluation code to preserve — its `function_app.py` is an
empty `FunctionApp()` — so nothing is lost by retiring the standalone Function App. This is the
same folder-restructuring decision flagged in `04-activity-normalize.md#alignment-gaps-vs-current-code`;
see the top-level `../README.md#open-questions--decisions-required`.
