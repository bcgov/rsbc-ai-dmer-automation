
# Testing Guide

> **Note:** `rule-engine` and `normalizer-service` below refer to the original architecture's
> standalone services. Under the revised architecture they are activities inside
> `workflow-orchestrator`/the driver-orchestration service — see
> [`README.md`](README.md#open-questions--decisions-required) for the current service mapping. The
> golden-file testing approach and coverage targets described here still apply to that code,
> wherever it ends up living, and the Driver Orchestration join additionally needs the deliberate
> concurrency test described in
> [`stages/06-driver-orchestration.md`](stages/06-driver-orchestration.md#implementation-considerations-for-claude-code).

## Test layers

| Layer | Scope | Location |
|---|---|---|
| Unit | Pure functions, mapping/validation logic, rule evaluation | `services/<svc>/tests/unit` |
| Integration | Real (or emulated) Storage/Service Bus/PostgreSQL, mocked external systems (Mercury, OpenAI) | `services/<svc>/tests/integration` |
| Contract | Message schema compatibility across producer/consumer services | `tests/contract` |
| End-to-end | Full pipeline through a synthetic DMER in a dev environment | `tests/e2e` |

## Coverage gates

Minimum 80% line coverage per service, enforced in `test.yml`. Rule engine and normalizer
mapping logic target 95%+ given their impact on decision outcomes.

## Golden files

`rule-engine` and `normalizer-service` tests use golden input/output fixtures per rule
version so a `rules.json` change or prompt change shows an explicit, reviewable diff in
expected outputs.
