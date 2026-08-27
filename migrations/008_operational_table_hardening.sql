-- migrations/008_operational_table_hardening.sql
--
-- Forward-only hardening of the five canonical object-storage-backed
-- operational/control tables migrations/007_object_storage_raw_contract.sql
-- (renumbered from 006 -- see that file's own header note) introduced:
-- ingestion_run, ingestion_page, ingestion_cursor, rejected_record,
-- schema_observation, all in :"ops_schema". Never rewrites 001-007 --
-- migrations are immutable once applied; this migration only ADDS
-- constraints, an index, and tightens one role's grants -- it never
-- alters or drops anything 001-007 already created.
--
-- Four independent additions, each idempotent on its own:
--
-- 1. `ingestion_page.checksum` format validation -- this repository's
--    checksum standard, everywhere else it appears (raw artifact
--    verification in api_client.py/raw_loader.py, page verification in
--    object_storage/verify.py), is a lowercase hex-encoded SHA-256
--    digest (64 hex characters). A CHECK constraint enforces that shape
--    at the database layer too, as a second line of defense alongside
--    the application-level verification `object_storage/verify.py`
--    already performs before this table is ever written to.
--
-- 2. `rejected_record.reason_code` / `rejected_record.detail` bounded
--    validation -- migrations/006_record_quarantine.sql already applies
--    exactly this pattern (a fixed reason-code allowlist plus a bounded
--    detail length) to the legacy quarantine table; this migration
--    brings `rejected_record` up to the same standard. `reason_code`'s
--    allowlist is `endpoint_contract.RejectReason`'s exact five string
--    values (`not_an_object`, `unsupported_endpoint`, `missing_source_id`,
--    `missing_source_timestamp`, `invalid_source_timestamp`) -- adding a
--    sixth reason in Python requires a new forward-only migration here
--    too, by design (see that enum's own "never change the string value
--    of an existing member" docstring). `detail` is bounded at 500
--    characters (wider than quarantine's 200, since these detail
--    strings interpolate an endpoint name and a source field name --
--    see endpoint_contract.py's `Rejected` construction sites -- and
--    should never approach either bound in practice; both exist purely
--    to guarantee "sanitized and bounded", never to constrain a
--    legitimate message).
--
-- 3. Tightened `:"ingest_role"` grants on `rejected_record` -- least
--    privilege, matching quarantined_records' already-established,
--    stricter pattern (INSERT only) instead of the SELECT/INSERT/UPDATE
--    migrations/007 granted every one of the five new tables uniformly.
--    `state.insert_rejected_records` (the only code that ever touches
--    this table from the ingest role) only ever INSERTs -- reconciliation
--    counts come from the INSERT's own affected-row count within the
--    same transaction, never a SELECT against this table back (the same
--    reasoning migrations/006_record_quarantine.sql already documents
--    for quarantined_records). `:"transform_role"` and PUBLIC remain
--    exactly as migrations/007 already left them (no access at all) --
--    this migration does not touch either. No operational "rejected-
--    record reviewer" role exists yet in this repository's role model;
--    an operator must explicitly `CREATE ROLE`, `GRANT SELECT ON
--    :"ops_schema".rejected_record TO <that role>`, and scope it to a
--    specific person/process before anyone can read this table -- the
--    same documented default-deny posture migrations/006_record_quarantine.sql
--    already established for quarantined_records (see docs/RUNBOOK.md
--    "Rejected records").
--
-- 4. `ingestion_page_status_idx` -- migrations/007 already indexes
--    `ingestion_page` by `run_id` alone; this adds `(status, run_id)` to
--    support the operator query "every page currently in a given status
--    (e.g. every `failed` page) across every run", not just "every page
--    for one already-known run" (see docs/RUNBOOK.md "Operator
--    queries").
--
-- Deliberately NOT changed by this migration (documented here, not
-- altered): the four foreign keys migrations/007 already created
-- (`ingestion_page.run_id`, `rejected_record.run_id`,
-- `ingestion_cursor.successful_run_id`,
-- `schema_observation.first_observed_run_id`/`last_observed_run_id`, all
-- `REFERENCES ingestion_run (run_id)`) were created with PostgreSQL's
-- default `ON DELETE NO ACTION` -- a deliberate choice, not an
-- oversight: deleting an `ingestion_run` row while any page, rejection,
-- cursor, or schema-observation row still references it is refused by
-- PostgreSQL outright, so operational audit history can never disappear
-- as a side effect of deleting (or cascading a delete into) a run row.
-- This repository has no code path that ever deletes an `ingestion_run`
-- row at all; if one is ever added, it must reckon with this constraint
-- explicitly rather than this migration silently choosing `CASCADE` or
-- `SET NULL` on its behalf.
--
-- Idempotent: CHECK constraints are added inside DO blocks that first
-- check `pg_constraint` (PostgreSQL has no `ADD CONSTRAINT IF NOT
-- EXISTS`), the index uses `CREATE INDEX IF NOT EXISTS`, and the
-- REVOKE/GRANT pair is itself idempotent in PostgreSQL (re-applying an
-- already-applied grant/revoke is a safe no-op, not an error) --
-- rerunning this migration against an already-migrated database is a
-- safe no-op.

-- --- 1. ingestion_page.checksum format -----------------------------------

DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'ingestion_page_checksum_format_check'
      AND conrelid = to_regclass(format('%I.%I', :'ops_schema', 'ingestion_page'))
  ) THEN
    EXECUTE format(
      'ALTER TABLE %I.%I ADD CONSTRAINT ingestion_page_checksum_format_check '
      || 'CHECK (checksum ~ ''^[0-9a-f]{64}$'')',
      :'ops_schema', 'ingestion_page'
    );
  END IF;
END
$do$;

-- --- 2. rejected_record.reason_code / detail bounds ----------------------

DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'rejected_record_reason_code_check'
      AND conrelid = to_regclass(format('%I.%I', :'ops_schema', 'rejected_record'))
  ) THEN
    EXECUTE format(
      'ALTER TABLE %I.%I ADD CONSTRAINT rejected_record_reason_code_check '
      || 'CHECK (reason_code IN (''not_an_object'', ''unsupported_endpoint'', ''missing_source_id'', '
      || '''missing_source_timestamp'', ''invalid_source_timestamp''))',
      :'ops_schema', 'rejected_record'
    );
  END IF;
END
$do$;

DO $do$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'rejected_record_detail_length_check'
      AND conrelid = to_regclass(format('%I.%I', :'ops_schema', 'rejected_record'))
  ) THEN
    EXECUTE format(
      'ALTER TABLE %I.%I ADD CONSTRAINT rejected_record_detail_length_check '
      || 'CHECK (detail IS NULL OR char_length(detail) <= 500)',
      :'ops_schema', 'rejected_record'
    );
  END IF;
END
$do$;

-- --- 3. Least-privilege grants: ingest_role is INSERT-only on rejected_record ---

-- migrations/003_roles_and_grants.sql's `ALTER DEFAULT PRIVILEGES IN
-- SCHEMA :"ops_schema" GRANT SELECT, INSERT, UPDATE ON TABLES TO
-- :"ingest_role"` and migrations/007's own explicit grant both gave
-- :"ingest_role" SELECT/UPDATE on rejected_record -- neither is ever
-- exercised by this connector's own code (see module comment above), so
-- both are revoked here and replaced with INSERT-only, matching
-- quarantined_records' already-established least-privilege pattern.
REVOKE ALL ON :"ops_schema".rejected_record FROM :"ingest_role";
GRANT INSERT ON :"ops_schema".rejected_record TO :"ingest_role";
GRANT USAGE ON SEQUENCE :"ops_schema".rejected_record_id_seq TO :"ingest_role";

-- --- 4. ingestion_page status index for cross-run investigation ----------

CREATE INDEX IF NOT EXISTS ingestion_page_status_idx
  ON :"ops_schema".ingestion_page (status, run_id);
