
# Incident Response

## Severity definitions

- **Sev 1** — DMER intake pipeline fully down, or data integrity risk (e.g. incorrect
  decision written to Mercury).
- **Sev 2** — Partial pipeline degradation (single service down, DLQ accumulating) with
  no data integrity risk.
- **Sev 3** — Elevated errors/latency, pipeline still functioning within SLA.

## Response

1. Acknowledge the Azure Monitor alert.
2. Check `docs/operations/troubleshooting.md` for the relevant known procedure.
3. For Sev 1/2, open an incident channel and post correlation IDs / affected `mercuryCaseId`s.
4. Root-cause and remediate; for Sev 1, prepare a written post-incident review.

## Escalation

Escalation contacts and paging policy are maintained outside this repository (ops on-call
tool) — do not store personal contact information here.
