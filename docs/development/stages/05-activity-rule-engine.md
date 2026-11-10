# Activity: Rule Engine

Source: architecture doc §4.3.3. Part of [Document Orchestration](03-document-orchestration.md).
Data model: `../data-model.md`.

## Purpose

Evaluates the normalized document and receives a list of candidate outcomes. A validation step
then selects the highest-priority outcome.

## Decision engine

`DECISION_ENGINE` chooses what produces the candidates:

| Value | Candidates from |
|---|---|
| `triage_llm` (default) | The triage LLM (`dmer_common.triage`): one model call that reads the normalized DMER and the Triage Sort Procedures, as text generated from the PDF. Each condition it marks `APPLIED` is a candidate; a `POLICY_GAP`, or the model asking for human review, is an `IN` candidate. |
| `rules` | The active `rules.json`, evaluated in-process with GoRules ZEN (`dmer_common.rules`) — kept so the pipeline can switch back by changing the setting. |

Everything after the candidates is shared: the same `select_outcome()` picks the outcome (highest
action wins; FIT letters combine; no candidate is `IN`; a clean pass with a driving-record row is
`IN`), and the same unit of work records it. The orchestration, `rule_evaluation`, the `RULES`
stage run, `RULES_APPLIED` and the Decision Gateway do not change with the engine.

The triage LLM is the POC's (`docs/research/llm-triage-poc/`), unchanged: the same prompt (only
the procedures version is filled in from the PDF), the same request (identity fields and their
evidence removed, date of birth sent as age at exam, the diabetes-treatment flag only with
diabetes, content parts with the checklist of true conditions), and the deployment's default
temperature. Its driving-record check uses the same fields as the `rules.json` rows, not the
model's view.

### The procedures text

The PDF is maintained in the `rules` container (`active/triage_sort_procedures.pdf`), like
`rules.json`, and is read-only to the pipeline. Per document the activity reads only the PDF's
ETag. When the PDF is replaced, its text is generated once — converted with pdfplumber and
verified against the PDF word by word and column by column (`dmer_common.triage.policy_text`) —
and stored in the `triage-policy` container as `<pdf sha256>.txt`, where every other worker reads
it. A text that does not verify is never stored or used. Check a new PDF before publishing it with
`scripts/dev/build_triage_policy_text.py`.

The procedures' "Updated" date is the version: it must be registered in `rules_version` (by
maintenance, as for `rules.json`) and is recorded on the evaluation.

## Why this is a library call, not a service

The rule engine (GoRules/Zen) is a **library**, not a service — it needs no container and no
network hop. It runs in-process inside the Durable activity. This is a change from the original
architecture, where `rule-engine` was its own HTTP-triggered Azure Function invoked by the
orchestrator — see [Alignment gaps](#alignment-gaps-vs-current-code).

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable activity function, running GoRules/Zen in-process, called from the Document Orchestrator |
| Reads | `normalized-dmer` blob; from the `rules` container, the active procedures PDF (`triage_llm`) or `rules.json` (`rules`) |
| Writes | `rule_evaluation`, `dmer_document`, `dmer_stage_run`; with `triage_llm`, the model's full answer to `normalized-dmer/<document_id>/runs/<run_id>/triage.json` and, once per PDF version, the procedures text to `triage-policy` |

## Processing steps

1. Load the active `rules.json` from the `rules` container (see `../services/azure-blob-storage.md`
   for the `rules/active/` vs. `rules/versions/<version>/` layout).
2. Evaluate the normalized document against it, producing a list of candidate outcomes.
3. Select the highest-priority outcome per the validation step.
4. Persist **all** candidate outcomes, not just the winner, to `rule_evaluation`.
5. Increment the driver's `completed_document_count` (the `driver_evaluation` row was created or
   attached earlier in the same orchestration by
   [Driver Lookup](03-document-orchestration.md#activity-driver-lookup)).
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
| `rule_evaluation` | `INSERT` (one row **per evaluation**, not per document — a re-run creates a new row) | `document_id`, `rules_version`, `all_outcomes` (jsonb — `{"rule_engine_outcome_code": ..., "candidates": [...]}`: every candidate with its inputs, and the engine's own outcome), `selected_outcome_code`, `selected_reason`, `priority_rank`, `evaluated_at`, and (V0007) `fit_letter` — whether a FIT letter applies to the outcome. When a clean pass needs the driving-record check, `selected_outcome_code` is `IN` while `rule_engine_outcome_code` keeps the engine's `CP`, and `selected_reason` says why. |
| `rule_evaluation` (`triage_llm`) | as above | The same columns. `all_outcomes` is the triage LLM's decision record: `engine`, `prompt_version`, `policy_sha256`, `policy_updated`, `evaluation_status`, `rule_engine_outcome_code`, `candidates`, per condition its `rule_status`, `recommended_action`, `fit_letter` and `policy_reference` (no evidence or reasoning), `checks` (soft problems with the answer, by field and page name only) and `answer_blob_url` (the full answer, which quotes the DMER). `rules_version` is the procedures' "Updated" date. |
| `rules_version` | **Read only** | With `triage_llm`, the activity checks the procedures' "Updated" date is a registered `version` (`RulesetUnavailableError` if not). With `rules`, it finds the active ruleset's row by its `checksum` (sha256) and records its `version` on the evaluation. It never writes this table: rows are maintained by whoever publishes or corrects `rules.json` (see [`rules_version`](../data-model.md#rules_version)). No matching row means the ruleset isn't registered — `RulesetUnavailableError`, logged at ERROR with the checksum, and the document is not evaluated. |
| `dmer_document` | `UPDATE` | `pipeline_status = RULES_APPLIED`, then `AWAITING_DRIVER_COMPLETION` once the driver is signalled (not built yet — the driver-decision publish needs Driver Lookup); `current_stage = DECISION`, `updated_at`. |
| `driver_evaluation` | `UPDATE` | `completed_document_count` incremented — this counter and `expected_document_count` are what make the wait observable. Only when a `driver_evaluation_document` marker row is newly inserted (see [Idempotency](#idempotency-requirements)); a document with no open evaluation is completed but not counted (logged as a warning). |
| `driver_evaluation_document` | `INSERT ... ON CONFLICT DO NOTHING` (V0004) | `driver_evaluation_id`, `document_id`, `counted_at` — the once-per-document marker. |
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = RULES`, `status`, `attempt_no`, timings; the rules version goes in `model_version` (the stage's version column, as Normalize records its model there). |

The evaluation row, stage run, document status and driver count are written in **one transaction**,
on a connection holding a per-document advisory lock (`dmer_common.db.rule_engine`) — the same
unit-of-work pattern as Normalize.

## Precedence: document_priority vs. rule engine

Question I-3 (answered): **the rule engine decides PR/PU/TCM/CR entirely from medical content.**
`dmer_document.document_priority` (from Mercury) does not override the outcome selection.

## Failure handling

With `triage_llm`: an unreadable procedures PDF, a generated text that does not verify, an
unregistered procedures version, or a `DECISION_ENGINE` that is neither engine is
`RulesetUnavailableError` — every document, logged at ERROR, retried like a missing `rules.json`.
A model answer that is still invalid after its retries (`NORMALIZATION_MODEL_ATTEMPTS`) is poison
for that document (manual review). Soft problems — evidence that doesn't quote the input, unknown
fields, impossible pages — are recorded in `all_outcomes.checks` and never retried.

With `rules`:

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
| `DECISION_ENGINE` | `triage_llm` (default) or `rules` — see [Decision engine](#decision-engine). |
| `TRIAGE_POLICY_PDF_PATH` | Path of the procedures PDF **within** `RULES_CONTAINER`, default `active/triage_sort_procedures.pdf`. |
| `TRIAGE_POLICY_CONTAINER` | Where the generated procedures text is kept, default `triage-policy` (written by this app only). |
| `TRIAGE_TEMPERATURE` | Optional; unset leaves it to the deployment (as validated in the POC). |
| `RULES_CONTAINER` | Default `rules`. |
| `RULES_ACTIVE_PATH` | Path **within** `RULES_CONTAINER`, default `active/rules.json` (i.e. `rules/active/rules.json`). |
| `RULE_ENGINE_LIBRARY` | GoRules ZEN Python binding: `zen-engine` (imported as `zen`), pinned `>=0.51,<0.52` in `libs/dmer_common/pyproject.toml`. |

## Idempotency requirements

Covered by orchestrator replay semantics, same as Normalize. The one caveat: `completed_document_count`
must only be incremented **once per document**, even if the activity is retried within an attempt —
increment via a `driver_evaluation_document` join/marker or an idempotent `UPSERT` keyed on
`(driver_evaluation_id, document_id)`, not a bare `UPDATE ... SET completed_document_count = completed_document_count + 1`
that would double-count on activity retry before the orchestrator's own dedup kicks in.
**Implemented** with the marker table (V0004): the count is incremented only when the marker row
is newly inserted, so a retry — or a later re-run of the rules for the same document — never
counts it twice. A retried invocation after a committed success returns the committed evaluation
without re-evaluating.

## Logging / auditing

Log `rules_version` and `selected_outcome_code` on every evaluation (not the full `all_outcomes`
payload at INFO level — it can carry section-level content close to clinical data; keep it in the
DB row, not the log stream).

## Implementation considerations for Claude Code

- The evaluation library is `dmer_common.rules` (`Ruleset(content).evaluate(dmer, received_date=...)`
  returns every candidate plus the selected outcome). `received_date` comes from
  `dmer_document.received_date`, not the normalized document -- the monocular rules measure DMER
  age from it. A clean pass that any Drugs, Alcohol and Driving row fired for is selected as `IN`
  (the 5-year driving record needs a human check); the engine's own outcome is kept as
  `rule_engine_outcome_code`. No rows fired at all selects `IN`.
- The ruleset lives at `services/rule-engine/rules/rules.json` (mirrors `rules/active/rules.json`
  in Blob Storage) and is exercised by `libs/dmer_common/tests/test_rules_engine.py`.
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
