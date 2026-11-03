# Sample rules

Local sample `rules.json` for development/testing, mirroring the schema stored in Blob Storage under `rules/active/rules.json` in each environment.

`rules.json` here is the current triage ruleset (a GoRules ZEN decision graph built from the
Triage Sort Procedures). It's evaluated by `dmer_common.rules` and covered by
`libs/dmer_common/tests/test_rules_engine.py`, which reads this file -- run those tests after
any edit. Every chapter table uses the `collect` hit policy and outputs `action`,
`fit_letter` and `reason` (plus `driving_record_check` on the Drugs, Alcohol and Driving rows);
the highest-priority action across all tables is selected.
