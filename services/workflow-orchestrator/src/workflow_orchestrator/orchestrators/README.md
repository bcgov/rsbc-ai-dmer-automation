# Orchestrators

Durable orchestrator functions — deterministic only, no direct I/O. See docs/development/coding-standards.md.

- **Document Orchestration** (`document_orchestration.py`) — see
  `docs/development/stages/03-document-orchestration.md`. Currently calls
  `NormalizeDmer` only and returns its result; `RunRuleEngine` and the
  driver-decision publish that follows it in the documented flow are not
  built yet (see `../activities/README.md`). Started by the `dmer-extracted`
  queue trigger in `../function_app.py` (`instance_id = document_guid`).
