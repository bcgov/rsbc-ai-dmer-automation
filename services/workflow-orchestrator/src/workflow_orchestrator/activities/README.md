# Activities

Durable activity functions for the Document Orchestration stage (see
docs/development/stages/03-document-orchestration.md).

- **Normalize** (`normalize.py`) -- calls Azure OpenAI to derive rule-ready
  fields from the extracted content. Registered; the actual logic lives in
  `dmer_common.normalization` (see docs/development/stages/04-activity-normalize.md),
  this module is just the thin trigger + blob/DB wiring.
- **Rule Engine** (not built yet) -- evaluates the normalized document
  against `rules.json` (GoRules/Zen), in-process, no container/network hop
  (see docs/development/stages/05-activity-rule-engine.md).

This previously described a different, single-orchestration design
(metadata loading, duplicate validation, case creation, Mercury update,
completion) that the revised architecture superseded -- that work now
belongs to Driver Orchestration (`06-driver-orchestration.md`), a separate
per-driver service, not this per-document one. See
`03-document-orchestration.md#alignment-gaps-vs-current-code` for the full
history.
