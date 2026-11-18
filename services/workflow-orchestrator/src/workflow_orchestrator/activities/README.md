# Activities

Durable activity functions for the Document Orchestration stage (see
docs/development/stages/03-document-orchestration.md).

- **Driver Lookup** (`driver_lookup.py`) -- first: keeps the driver Mercury
  supplied, or looks up the licence read off the page with Mercury's
  `GET by driver_licence`; creates or attaches the driver's open
  `driver_evaluation` with `expected_document_count`. Unresolvable documents
  go to `MANUAL_REVIEW`. DB unit of work: `dmer_common.db.driver_lookup`.
- **Normalize** (`normalize.py`) -- calls Azure OpenAI to derive rule-ready
  fields from the extracted content. Registered; the actual logic lives in
  `dmer_common.normalization` (see docs/development/stages/04-activity-normalize.md),
  this module is just the thin trigger + blob/DB wiring.
- **Rule Engine** (`rule_engine.py`) -- evaluates the normalized document
  against the active `rules.json` (GoRules/Zen), in-process, no
  container/network hop, and records every candidate outcome in
  `rule_evaluation` (see docs/development/stages/05-activity-rule-engine.md).
  The evaluation is `dmer_common.rules`; the DB unit of work is
  `dmer_common.db.rule_engine`.

- **Signal Driver** (`signal_driver.py`) -- last: publishes `driver-decision`
  (`SessionId = driver_key`) and moves the document to
  `AWAITING_DRIVER_COMPLETION`.
- **Route To Manual Review** (`manual_review.py`) -- called by the orchestrator
  when an activity returns a poison result or exhausts its retries; sets
  `MANUAL_REVIEW` with a reason code.

Errors retrying can't fix are returned as `{"poison": true, "error_code": ...}`
(each module's `POISON`, via `_runtime.poison_as_result`) rather than raised,
so the orchestrator routes them to manual review instead of retrying.

This previously described a different, single-orchestration design
(metadata loading, duplicate validation, case creation, Mercury update,
completion) that the revised architecture superseded -- that work now
belongs to Driver Orchestration (`06-driver-orchestration.md`), a separate
per-driver service, not this per-document one. See
`03-document-orchestration.md#alignment-gaps-vs-current-code` for the full
history.
