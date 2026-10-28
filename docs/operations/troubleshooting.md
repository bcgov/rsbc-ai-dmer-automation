
# Troubleshooting

## Dead-lettered messages

The [DLQ Drain](../development/stages/10-dlq-drain.md) already classifies and routes every
dead-lettered message automatically; this runbook is for the operator triage that follows an alert.
Use the same four categories the drain uses.

1. Identify the queue/subscription with `$DeadLetterQueue` depth > 0 (alert fires automatically).
2. Inspect the message; a copy of the payload is also written to `failed/<queue-name>/...` in Blob Storage by `post-processing`. Read the matching `processing_error` row for `failure_category` and `reason_code`.
3. Confirm the category the drain assigned:
   - **`PERMANENT_BUSINESS`** (unreadable PDF, `document_guid` gone, content schema violation) — the drain has already written a fallback `IN` (`decided_by = FALLBACK`) and set `MANUAL_REVIEW`; the document is visible to Intake. No redrive needed; the follow-up is human reading, not a resubmit.
   - **`TRANSIENT`** (lock expiry, DB blip, network) — **no** decision was written (by design). Confirm the transient condition has cleared, then redrive.
   - **`PROCESSING`** (dependency 429/5xx/outage) — **no** decision was written. Fix or wait out the dependency, then redrive.
   - **`UNKNOWN`** — the drain could not classify it; investigate manually. Decide whether it is truly permanent (resolve to a fallback with `decided_by = MANUAL`) or a redrive candidate.
4. For `TRANSIENT`/`PROCESSING`/`UNKNOWN`, fix and redrive via the Service Bus DLQ resubmit tooling in `scripts/ci/` (or Azure Portal for a one-off). **Never manually post an `IN` for a transient or unknown failure** — that is precisely the unsafe behaviour the categorized drain exists to prevent (see [ADR-0002](../architecture/decision-records/0002-dlq-fallback-decisions-gated-by-failure-category.md)). Redrive the message and let the pipeline decide, or escalate.

## Document Intelligence throttling

Check Application Insights for `429` responses from `di-processor`; confirm the S0/S1 tier
transaction limits against current volume; consider request-rate smoothing via the
`raw-dmer-queue` KEDA scale rule (max concurrent messages).

## Normalization confidence drops

Check `normalization_results.confidence_score` trend in PostgreSQL; compare against the
`normalizer-service` prompt/model version — see `docs/services/normalizer-service.md`.

## Mercury integration failures

All Mercury calls go through `libs/dmer_common/mercury_client`; check its structured logs
for the specific Mercury endpoint and status code. Mercury outages should surface as
dead-lettered `raw-dmer-queue` or failed `workflow-orchestrator` "Mercury update" activities,
not silent data loss — both are retried and then dead-lettered.
