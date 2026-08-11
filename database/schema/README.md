
# Schema

Entity-relationship reference for the DMER PostgreSQL database (single database, one
schema per bounded context if needed later — starts as `public`).

## Recommended tables

| Table | Owned by (writer) | Purpose |
|---|---|---|
| `documents` | di-processor | One row per ingested DMER: source, blob paths, current status |
| `document_hash` | di-processor | SHA-256 hash index for de-duplication lookups |
| `duplicate_checks` | workflow-orchestrator | Result of each duplicate-validation check against `document_hash` |
| `processing_history` | workflow-orchestrator | Append-only state transition log per document |
| `normalization_results` | normalizer-service | Structured ontology output + confidence scores |
| `rule_execution` | rule-engine | Rule version, input snapshot reference, decision, reason codes |
| `audit_log` | post-processing | Append-only audit trail (who/what/when) for compliance and the Mercury-hosted dashboard |
| `dead_letter_events` | post-processing (or a small ops consumer) | Metadata captured from Service Bus DLQs for operational triage |

`audit-service` reads from all of the above; it does not write to any of them (CQRS-style
read/write separation — see `docs/services/audit-service.md`).

An ERD diagram should be added here (`erd.png` or a `mermaid` block) once the schema is finalized.
