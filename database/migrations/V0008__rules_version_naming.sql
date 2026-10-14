-- V0008__rules_version_naming.sql
--
-- rules_version is maintained by hand, never by the pipeline
-- (docs/development/data-model.md "rules_version"): its version is the ISO date
-- of the procedures the rules implement, plus ".N" for a later file of the same
-- procedures -- e.g. 2026-06-12, 2026-06-12.2.
--
-- Before this, the Rule Engine activity registered any unknown ruleset itself
-- as "sha256-<first 12 hex>". Those rows are folded into 2026-06-12 -- the
-- Triage Sort Procedures every ruleset so far implements: the most recent one's
-- blob_url and checksum are kept, every evaluation and RULES stage run that
-- referenced one of them is re-pointed, and they are removed. An environment
-- with none (nothing evaluated yet) is unchanged. Then the naming is enforced.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM rules_version WHERE version LIKE 'sha256-%') THEN
        INSERT INTO rules_version (version, blob_url, checksum, activated_at, activated_by)
        SELECT '2026-06-12', blob_url, checksum, activated_at,
               'V0008 (folded rule-engine-activity sha256 registrations)'
        FROM rules_version
        WHERE version LIKE 'sha256-%'
        ORDER BY activated_at DESC NULLS LAST
        LIMIT 1
        ON CONFLICT (version) DO NOTHING;

        UPDATE rule_evaluation SET rules_version = '2026-06-12'
        WHERE rules_version LIKE 'sha256-%';

        -- The RULES stage run records the rules version as its model_version.
        UPDATE dmer_stage_run SET model_version = '2026-06-12'
        WHERE stage = 'RULES' AND model_version LIKE 'sha256-%';

        DELETE FROM rules_version WHERE version LIKE 'sha256-%';
    END IF;
END $$;

DO $$ BEGIN
    ALTER TABLE rules_version
        ADD CONSTRAINT ck_rules_version_version
        CHECK (version ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}(\.[0-9]+)?$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON TABLE rules_version IS
    'One row per published rules.json. Written by maintenance only, never by the '
    'pipeline; version = ISO procedures date (+ .N revision). See data-model.md.';
