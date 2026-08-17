-- migrations/006_record_quarantine.sql
--
-- Forward-only addition: a restricted quarantine table for source
-- records that fail the structural ingestion contract (see
-- src/tuva_ingest/validators.py, quarantine.py, paginated_loader.py).
-- Never rewrites 001-005 -- migrations are immutable once applied.
--
-- *** This table contains PHI. ***
-- Structurally invalid records are still real source records -- a
-- record missing a required identifier field can still contain a name,
-- date of birth, diagnosis code, or any other PHI-bearing value the
-- source sent. Deployments must apply the same retention, auditing,
-- access-logging, and encryption-at-rest controls to this table that
-- they apply to the raw schema itself; this migration only configures
-- database-level access control (see grants below), not the
-- infrastructure-level controls (disk encryption, backup retention,
-- audit logging of SELECTs) a real deployment environment must supply.
--
-- Access model, deliberately more restrictive than the raw schema:
--   * PUBLIC has no access at all (explicit REVOKE, defense in depth on
--     top of PostgreSQL's already-restrictive default of no grants to
--     PUBLIC on a newly created table -- stated explicitly here so the
--     intent is never ambiguous to a future reader of this file).
--   * :"transform_role" (dbt) is never granted any access -- quarantined
--     records must never be reachable from a dbt model, directly or
--     indirectly; there is no models/sources.yml entry for this table
--     and there must never be one.
--   * :"ingest_role" (this Python connector) is granted only INSERT --
--     not SELECT, UPDATE, or DELETE. The connector's own reconciliation
--     logic (paginated_loader.py) counts quarantined rows using the
--     in-transaction INSERT ... RETURNING / row-count it already has
--     from the insert it just performed, so it never needs to SELECT
--     this table back. A future operational "quarantine review" role is
--     intentionally left ungranted here (see README.md/docs/RUNBOOK.md
--     "Quarantined records" -- no such role exists yet in this
--     repository's role model; an operator must explicitly create and
--     grant one before anyone can read this table, which is the
--     intended default-deny posture for a PHI-bearing table with no
--     established reviewer role).
--
-- Idempotent: guarded by IF NOT EXISTS; REVOKE/GRANT are themselves
-- idempotent in PostgreSQL (re-applying an already-applied
-- grant/revoke is a safe no-op, not an error).

CREATE TABLE IF NOT EXISTS :"ops_schema".quarantined_records (
  quarantine_id       bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id              text NOT NULL,
  source              text NOT NULL,
  endpoint            text NOT NULL,
  page_number         integer NOT NULL,
  record_index        integer NOT NULL,
  reason_code         text NOT NULL,
  reason_detail        text,
  raw_record           jsonb NOT NULL,
  source_record_sha256 text NOT NULL,
  quarantined_at        timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT quarantined_records_reason_code_check CHECK (
    reason_code IN (
      'record_not_object',
      'missing_required_field',
      'invalid_required_type',
      'invalid_identifier',
      'invalid_date_format',
      'schema_validation_failed'
    )
  ),
  CONSTRAINT quarantined_records_reason_detail_length_check CHECK (
    reason_detail IS NULL OR char_length(reason_detail) <= 200
  )
);

-- One quarantine row per (run_id, page_number, record_index) -- the
-- exact same idempotency shape the raw tables use
-- (migrations/005_paginated_extraction_state.sql), so a repeated
-- `tuva-ingest load --run-id <same value>` never duplicates a
-- quarantine row either.
CREATE UNIQUE INDEX IF NOT EXISTS quarantined_records_run_page_record_key
  ON :"ops_schema".quarantined_records (run_id, page_number, record_index);

-- Read access by run/endpoint -- used only by the connector's own
-- reconciliation-count queries and by an operator's own ad hoc,
-- explicitly-granted review access (see access model above).
CREATE INDEX IF NOT EXISTS quarantined_records_run_id_idx
  ON :"ops_schema".quarantined_records (run_id);
CREATE INDEX IF NOT EXISTS quarantined_records_source_endpoint_idx
  ON :"ops_schema".quarantined_records (source, endpoint);

-- --- access control -------------------------------------------------

REVOKE ALL ON :"ops_schema".quarantined_records FROM PUBLIC;

-- migrations/003_roles_and_grants.sql's `ALTER DEFAULT PRIVILEGES IN
-- SCHEMA :"ops_schema" GRANT SELECT, INSERT, UPDATE ON TABLES TO
-- :"ingest_role"` applies automatically to this brand-new table too
-- (that is exactly what "default privileges" means) -- so the
-- unconditional REVOKE below is not defensive boilerplate, it is load-
-- bearing: without it, :"ingest_role" would silently end up with
-- SELECT/UPDATE on a PHI-bearing quarantine table, contradicting this
-- table's whole reason for being more restricted than the raw schema.
REVOKE ALL ON :"ops_schema".quarantined_records FROM :"ingest_role";
GRANT INSERT ON :"ops_schema".quarantined_records TO :"ingest_role";
GRANT USAGE ON SEQUENCE :"ops_schema".quarantined_records_quarantine_id_seq TO :"ingest_role";

-- :"transform_role" is deliberately granted nothing here -- see the
-- access model comment above. (It has no default-privilege grant on
-- :"ops_schema" in the first place -- migrations/003 only configures
-- :"transform_role" on :"raw_schema" -- but the REVOKE ALL above still
-- covers it explicitly for defense in depth.)
