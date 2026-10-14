-- V0007__rule_evaluation_fit_letter.sql
--
-- Whether a FIT letter applies to the evaluation's outcome
-- (docs/development/data-model.md "rule_evaluation"): true when any candidate
-- at the engine's winning action has fit_letter set (dmer_common.rules
-- select_outcome). The per-candidate flags stay in all_outcomes; this stores the
-- combined result so readers don't have to re-derive it. Additive only (expand
-- step). Null on evaluations recorded before this migration.

ALTER TABLE rule_evaluation ADD COLUMN IF NOT EXISTS fit_letter boolean;
