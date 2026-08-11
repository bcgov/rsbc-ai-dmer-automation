
# Troubleshooting

## Dead-lettered messages

1. Identify the queue/subscription with `$DeadLetterQueue` depth > 0 (alert fires automatically).
2. Inspect the message; a copy of the payload is also written to `failed/<queue-name>/...` in Blob Storage by `post-processing`.
3. Determine root cause (schema mismatch, downstream outage, poison message).
4. Fix and redrive via the Service Bus DLQ resubmit tooling in `scripts/ci/` (or Azure Portal for a one-off).

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
