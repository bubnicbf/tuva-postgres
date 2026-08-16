-- migrations/001_operational_schemas.sql
--
-- Creates the two schemas this connector ever writes to: the raw
-- landing schema (:"raw_schema" -- eligibility/medical_claim/
-- pharmacy_claim source data, see 002_ingestion_control.sql) and the
-- operational/control schema (:"ops_schema" -- ingestion run and
-- table-load history). Both names are operator-configurable
-- (RAW_SCHEMA/OPS_SCHEMA, see config.py), which is exactly why this
-- migration is applied through the Python migration runner
-- (src/tuva_ingest/migrations.py) rather than as static SQL: PostgreSQL
-- has no bind-parameter mechanism for a schema name, so the runner
-- validates both against the shared identifier policy
-- (src/tuva_ingest/identifiers.py) and substitutes them as psql-style
-- `:"name"` identifier variables (src/tuva_ingest/db.py's
-- substitute_psql_vars) before this file's SQL text is ever executed.
--
-- This migration deliberately owns nothing else: it never creates, and
-- this repository never maintains, any Tuva-managed core, terminology,
-- or output schema/table -- those are owned entirely by the pinned
-- tuva-health/the_tuva_project dbt package (see packages.yml).
--
-- Idempotent: CREATE SCHEMA IF NOT EXISTS is a safe no-op on rerun.

CREATE SCHEMA IF NOT EXISTS :"raw_schema";
CREATE SCHEMA IF NOT EXISTS :"ops_schema";
