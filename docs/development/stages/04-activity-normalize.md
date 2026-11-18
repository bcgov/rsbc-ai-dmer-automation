# Activity: Normalize

Source: architecture doc §4.3.2. Part of [Document Orchestration](03-document-orchestration.md).
Data model: `../data-model.md`.

## Purpose

Reads the combined extraction JSON, takes the handwritten fields, and calls Azure OpenAI with the
agreed key set to derive additional rule-ready fields. Validates the model output against the
schema, compares each derived value back against the source text it came from, and writes the
result to `normalized-dmer`.

## Why this is a plain activity, not a container app

Because the Azure OpenAI endpoint is reachable with a key, this is a **direct SDK call from the
activity** — no container app required. If the endpoint later moves to a private endpoint, an
Azure Function on Flex Consumption/Premium with VNet integration reaches private endpoints too, so
the hosting decision doesn't change either way. This is a deliberate change from the original
architecture, where `normalizer-service` was its own Container App — see
[Alignment gaps](#alignment-gaps-vs-current-code).

## Why Normalize is a separate stage from Extraction

Both call a model, but keep them separate. The normalization schema will change far more often than
the OCR pipeline, and separating them means a schema change can be replayed from the stored
extraction result **without paying for OCR a second time**. That replay capability is the entire
reason each stage writes its own blob.

## Trigger / reads / writes

| | |
|---|---|
| Type | Durable activity function, called from the Document Orchestrator |
| Reads | `extracted-dmer` blob |
| Writes | `normalized-dmer` blob, `dmer_document`, `dmer_stage_run` |

## Processing steps

1. Fetch the combined extraction JSON from `extracted-dmer` (via the blob URL passed as activity
   input — never the JSON content itself, per the orchestrator's determinism/history-size rule in
   [Document Orchestration](03-document-orchestration.md)).
2. Take the handwritten fields and call Azure OpenAI (GPT-5.1) with the agreed key set to derive
   additional rule-ready fields.
3. Validate the model output against the normalization schema.
4. Compare each derived value back against the source text it came from (evidence validation).
5. Write the result to `normalized-dmer`.

## Database writes

| Table | Operation | Fields |
|---|---|---|
| `dmer_stage_run` | `INSERT` then `UPDATE` | `stage = NORMALIZE`, `status`, `attempt_no`, `started_at`, `ended_at`, `output_blob_url` = the normalized blob, `model_version` (deployment name **and** normalization schema version). |
| `dmer_document` | `UPDATE` | `pipeline_status = NORMALIZED`, `current_stage = RULES`, `updated_at`. |
| `dmer_extraction` | `UPDATE` (optional) | If the team decides normalized clinical content may live in Postgres, store the normalized jsonb here. **Needs privacy sign-off first** — see `../data-model.md#open-questions--decisions-required`. Do not implement this column until that sign-off exists. |

## Failure handling

Same transient/poison split as every OpenAI call (see `../services/azure-openai.md`): 429s and
timeouts retry with backoff inside the activity; a schema-validation failure that can't be
resolved by retrying is poison — raise a terminal exception so the orchestrator routes the document
to `MANUAL_REVIEW` (see [Document Orchestration](03-document-orchestration.md#failure-handling)).

## Interaction with upstream/downstream

Reads Extraction's output (`extracted-dmer`). Feeds [Activity: Rule Engine](05-activity-rule-engine.md)
directly within the same orchestration instance — no queue between them.

## Configuration / environment variables

Same Azure OpenAI variables as Extraction — see `02-extraction.md` and `../services/azure-openai.md`.
Add a separate `NORMALIZATION_SCHEMA_VERSION` (or equivalent) so the normalization prompt/schema can
version independently of the extraction structuring prompt.

## Idempotency requirements

Covered by the orchestrator's replay semantics (see
[Document Orchestration](03-document-orchestration.md#how-durable-functions-actually-move-work)) —
this activity does not need its own idempotency key.

## Implementation considerations for Claude Code

- Reuse `libs/dmer_common/src/dmer_common/openai_client/client.py` (`OpenAIClient.complete()`) —
  already wraps retry (`openai_retry`) and circuit breaker (`openai_breaker`) tuned for external
  Azure OpenAI calls.
- This activity's code should live alongside the Document Orchestrator — see
  `03-document-orchestration.md#implementation-considerations-for-claude-code` for the
  recommended module boundary question.

## Alignment gaps vs. current code

`docs/services/normalizer-service.md` and `services/normalizer-service/` (placeholder Container App
folder) describe normalization as an independently deployable Container App, invoked over HTTPS by
the orchestrator, with its own `.env.example` and Dockerfile. Under the revised architecture,
normalization is an **in-process Durable activity inside `workflow-orchestrator`**, not a separate
compute resource. `services/normalizer-service/` has no working code to preserve (its `main.py` is
`raise NotImplementedError`), so nothing is lost by retiring it — but this is a real decision the
team needs to confirm: keep `normalizer-service` as an empty/retired folder, or delete it and fold
its intended logic into `workflow-orchestrator`. See the top-level
`../README.md#open-questions--decisions-required` for the consolidated folder-restructuring
question covering this and the other retired services.
