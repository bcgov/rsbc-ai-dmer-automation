-- roles.sql
--
-- Grants intake-processor's Function App managed identity access to every
-- table/sequence in the `dmer` database -- present and future, via
-- ALTER DEFAULT PRIVILEGES, so new tables don't need a repeat grant each
-- time one's added. Deliberately no DELETE: dmer_intake never deletes rows.
--
-- Run against the app database (`dmer`), after create-principal.sql (this
-- same directory) has been run against the server's default `postgres`
-- database -- see that file's header for why the two are split across
-- different databases and what its own prerequisite is. apply_roles.sh
-- runs both, against the right database each, in the right order.
--
-- Not run by any pipeline (CI/CD for this is still blocked -- see
-- docs/deployment/deployment-guide.md) -- apply by hand via apply_roles.sh
-- in this same directory, which fills in :function_app_name for you from
-- deployment/<env>/intake-processor.parameters.json rather than needing it
-- typed in here.
--
-- :function_app_name is double-quoted below (:"function_app_name") in
-- every use here, since it's always an identifier (a role name) in these
-- GRANT/ALTER statements -- contrast with create-principal.sql, where it's
-- single-quoted as a string-literal function argument instead.

GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO :"function_app_name";
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO :"function_app_name";

-- Only auto-covers tables/sequences later created by whichever role runs
-- this statement -- a different admin creating a table afterwards would
-- need to re-run this (or the GRANT ... ON ALL TABLES lines above) again.
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO :"function_app_name";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO :"function_app_name";
