-- migrations/003_roles_and_grants.sql
--
-- Least-privilege role separation between ingestion (this Python
-- connector) and transformation (dbt/the Tuva package):
--
--   :"ingest_role"    -- the role this connector's own database
--                         connection should ultimately run as (directly,
--                         or via `GRANT :"ingest_role" TO <login role>`
--                         performed separately by an operator/DBA -- this
--                         migration never creates a login role or a
--                         password, since a migration file must never
--                         contain a secret). Can read/write the raw
--                         schema's managed tables and the operational
--                         control schema; cannot touch the Input Layer or
--                         any Tuva-managed schema.
--   :"transform_role" -- the role dbt/the Tuva package should run as.
--                         Read-only on the raw schema (dbt only ever
--                         SELECTs from raw.* in the staging models, see
--                         models/staging/) and has no access to the
--                         operational control schema at all (dbt has no
--                         business reading/writing ingestion run state).
--
-- Roles are cluster-level objects with no `CREATE ROLE IF NOT EXISTS` in
-- PostgreSQL, so idempotent creation uses a catalog existence check (see
-- the two DO blocks below) -- the same pattern this repository's earlier
-- architecture used for idempotent constraint creation. Both roles are
-- created NOLOGIN (group/permission roles, not directly connectable) so
-- this migration never handles a password; GRANT-ing an actual login
-- role membership in one of these is a separate, operator-performed step
-- (see docs/RUNBOOK.md).
--
-- Idempotent: role creation is guarded by an existence check; every
-- GRANT below is itself idempotent in PostgreSQL (re-granting an
-- already-held privilege is a safe no-op, not an error).

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ingest_role') THEN
    EXECUTE format('CREATE ROLE %I NOLOGIN', :'ingest_role');
  END IF;
END
$do$;

DO $do$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'transform_role') THEN
    EXECUTE format('CREATE ROLE %I NOLOGIN', :'transform_role');
  END IF;
END
$do$;

-- --- ingest_role: read/write the raw schema + full control over ops_schema -

GRANT USAGE, CREATE ON SCHEMA :"raw_schema" TO :"ingest_role";
GRANT SELECT, INSERT, TRUNCATE ON ALL TABLES IN SCHEMA :"raw_schema" TO :"ingest_role";
ALTER DEFAULT PRIVILEGES IN SCHEMA :"raw_schema"
  GRANT SELECT, INSERT, TRUNCATE ON TABLES TO :"ingest_role";

GRANT USAGE, CREATE ON SCHEMA :"ops_schema" TO :"ingest_role";
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA :"ops_schema" TO :"ingest_role";
GRANT USAGE ON ALL SEQUENCES IN SCHEMA :"ops_schema" TO :"ingest_role";
ALTER DEFAULT PRIVILEGES IN SCHEMA :"ops_schema"
  GRANT SELECT, INSERT, UPDATE ON TABLES TO :"ingest_role";
ALTER DEFAULT PRIVILEGES IN SCHEMA :"ops_schema"
  GRANT USAGE ON SEQUENCES TO :"ingest_role";

-- --- transform_role: read-only on the raw schema, nothing else here ------
--     (its Input Layer/Tuva-package output schemas are created and
--     granted by dbt/the warehouse admin outside this connector's own
--     migrations -- this repository does not own that DDL; see
--     README.md "This repository does not own Tuva's DDL").

GRANT USAGE ON SCHEMA :"raw_schema" TO :"transform_role";
GRANT SELECT ON ALL TABLES IN SCHEMA :"raw_schema" TO :"transform_role";
ALTER DEFAULT PRIVILEGES IN SCHEMA :"raw_schema"
  GRANT SELECT ON TABLES TO :"transform_role";
