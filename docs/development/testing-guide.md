
# Testing Guide

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
