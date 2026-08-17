-- migrations/006_object_storage_raw_contract.sql
--
-- Forward-only addition supporting the object-storage-backed ingestion
-- workflow (see src/tuva_ingest/object_storage/, object_extract.py,
-- object_raw_loader.py, docs/SOURCE_CONTRACT.md "Object storage"). Never
-- rewrites 001-005 -- migrations are immutable once applied.
--
-- This migration does NOT replace anything migrations/002 or 005
-- created: ingestion_runs, table_loads, and source_watermarks (plural,
-- legacy) remain exactly as they are and continue to back the legacy
-- CSV workflow (raw_loader.py) and the local-filesystem paginated
-- workflow (pagination.py/paginated_loader.py) unchanged. The five
-- tables this migration adds are singular and net-new; they are the
-- sole operational/control model for the NEW object-storage-backed
-- workflow (src/tuva_ingest/object_extract.py/object_raw_loader.py)
-- only. In particular, ingestion_cursor (not source_watermarks) is the
-- one and only authoritative cursor source for that new workflow --
-- see state.py's get_cursor/commit_cursor and
-- docs/SOURCE_CONTRACT.md "Cursor safety" for why these two cursor
-- stores are never allowed to both back the same running workflow.
--
-- Four sections, all in either :"ops_schema" or :"raw_schema" (never
-- Tuva-managed -- staging_incoming/input_layer/analytics_core/
-- analytics_marts are created by dbt itself on `dbt run`/`dbt build`,
-- never by this connector's own migrations; see dbt_project.yml and
-- macros/generate_schema_name.sql):
--
-- 1. Re-assert :"raw_schema"/:"ops_schema" exist. migrations/001 already
--    did this once, but 001 is immutable and will never re-run -- an
--    existing database that upgrades to a new RAW_SCHEMA/OPS_SCHEMA
--    default (see config.py; the shipped defaults changed from
--    raw/ingest_ops to raw_incoming/ops as part of this same change)
--    needs this schema to actually get created somewhere. CREATE SCHEMA
--    IF NOT EXISTS is always a safe no-op when the schema already
--    exists under an unchanged, explicitly-configured name.
--
-- 2. Five new canonical operational/control tables in :"ops_schema":
--    ingestion_run, ingestion_page, ingestion_cursor, rejected_record,
--    schema_observation. See each table's own comment below for its
--    exact column contract (this mirrors the exact table/column list
--    documented in docs/SOURCE_CONTRACT.md "Operational tables").
--
-- 3. Seven new nullable raw-metadata columns on each of the three raw
--    landing tables (:"raw_schema".eligibility/medical_claim/
--    pharmacy_claim): _ingestion_run_id, _ingested_at, _source_endpoint,
--    _source_record_id, _source_updated_at, _payload_hash, _raw_payload
--    -- the exact seven columns every row written by
--    object_raw_loader.py must carry (see endpoint_contract.py for how
--    each is derived). Nullable, like every previous forward-compatible
--    raw-table addition in this repository (004, 005): a row loaded by
--    the legacy CSV contract or the local-filesystem paginated contract
--    leaves all seven NULL, which is a valid, permanent state, never a
--    "pending migration" for old rows. `_raw_payload` is deliberately a
--    SEPARATE column from the legacy `raw_row` (migrations/002) rather
--    than a rename or a shared column -- two independently populated
--    JSON payload columns never drift because each loader (legacy
--    raw_loader.py vs. new object_raw_loader.py) only ever writes its
--    own column; dbt's staging models bridge the two explicitly via
--    `coalesce(_raw_payload, raw_row)` (see models/staging/*.sql) --
--    the "explicit compatibility path" this repository's own
--    architecture requires instead of letting two payload columns
--    silently disagree.
--
--    A partial unique index enforces the source-stable idempotency rule
--    from docs/SOURCE_CONTRACT.md "Stable uniqueness rule":
--    (_source_endpoint, _source_record_id, _source_updated_at,
--    _payload_hash), scoped to rows the new loader actually populated
--    (WHERE _source_record_id IS NOT NULL) so it never applies to (and
--    can never conflict with) legacy-loaded rows, which always leave
--    _source_record_id NULL.
--
-- 4. Least-privilege grants for the five new ops tables: :"ingest_role"
--    gets SELECT/INSERT/UPDATE (the same shape migrations/003 already
--    grants on every other :"ops_schema" table) plus sequence USAGE for
--    their bigserial id columns. :"transform_role" gets nothing here --
--    migrations/003 already scopes :"transform_role" to
--    :"raw_schema"-read-only and nothing in :"ops_schema" at all (dbt
--    has no business reading operational/control state); this migration
--    explicitly REVOKEs on rejected_record as defense in depth (a
--    accidentally-broader future grant on :"ops_schema" must still never
--    reach this one PHI-bearing table). PUBLIC is explicitly revoked on
--    every PHI-bearing raw table and on rejected_record -- belt-and-
--    braces alongside PostgreSQL's own default-deny behavior for newly
--    created tables.
--
--    Creating a transaction-local TEMP TABLE (see object_raw_loader.py's
--    COPY-to-temp-then-merge pattern) requires only the TEMP privilege
--    on the *database* itself, which PostgreSQL grants to every role by
--    default (there is no per-schema TEMP privilege, and no database
--    name is available as a safely-substitutable identifier variable in
--    this migration's :"name" mechanism) -- an operator who has revoked
--    TEMP from PUBLIC on the target database must explicitly
--    `GRANT TEMP ON DATABASE <name> TO` :"ingest_role" themselves (see
--    docs/RUNBOOK.md "Least-privilege grants").
--
-- Idempotent: every statement uses IF NOT EXISTS (or, for constraints
-- PostgreSQL has no IF NOT EXISTS form for, a catalog existence check --
-- see the DO blocks below, the same pattern migrations/003 already
-- uses for idempotent role creation); rerunning this migration against
-- an already-migrated database is a safe no-op.

-- --- 1. Re-assert schema existence ---------------------------------------

CREATE SCHEMA IF NOT EXISTS :"raw_schema";
CREATE SCHEMA IF NOT EXISTS :"ops_schema";

-- --- 2. Canonical operational/control tables -----------------------------

-- One row per object-storage-backed extraction+load run. `run_id` is
-- always the same true UUID4 minted for the run's object-key run_id
-- component (see object_storage/keys.new_run_id) -- never re-derived or
-- re-minted here.
CREATE TABLE IF NOT EXISTS :"ops_schema".ingestion_run (
  run_id                uuid PRIMARY KEY,
  vendor                text NOT NULL,
  endpoint              text NOT NULL,
  load_date             date NOT NULL,

  storage_bucket        text,
  storage_run_prefix    text NOT NULL,

  requested_cursor      text,
  candidate_cursor      text,

  status                text NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'published', 'loading', 'committed', 'failed')),

  started_at            timestamptz NOT NULL DEFAULT now(),
  published_at          timestamptz,
  load_started_at       timestamptz,
  committed_at          timestamptz,
  failed_at             timestamptz,
  finished_at           timestamptz,

  extracted_count       bigint CHECK (extracted_count IS NULL OR extracted_count >= 0),
  accepted_count        bigint CHECK (accepted_count IS NULL OR accepted_count >= 0),
  rejected_count        bigint CHECK (rejected_count IS NULL OR rejected_count >= 0),
  inserted_count        bigint CHECK (inserted_count IS NULL OR inserted_count >= 0),
  duplicate_count       bigint CHECK (duplicate_count IS NULL OR duplicate_count >= 0),
  page_count            integer CHECK (page_count IS NULL OR page_count >= 0),

  failure_category      text,
  failure_message       text,

  app_version           text,
  environment            text
);

CREATE INDEX IF NOT EXISTS ingestion_run_endpoint_date_idx
  ON :"ops_schema".ingestion_run (vendor, endpoint, load_date DESC);

CREATE INDEX IF NOT EXISTS ingestion_run_status_idx
  ON :"ops_schema".ingestion_run (status, started_at DESC);

-- One row per published page of a run. `object_key` is globally unique
-- (every page object ever published, across every run, has exactly one
-- row) -- this is the immutable-object-key uniqueness rule from
-- docs/SOURCE_CONTRACT.md "Object storage".
CREATE TABLE IF NOT EXISTS :"ops_schema".ingestion_page (
  id                    bigserial PRIMARY KEY,
  run_id                uuid NOT NULL REFERENCES :"ops_schema".ingestion_run (run_id),
  page_number           integer NOT NULL CHECK (page_number BETWEEN 1 AND 999999),
  object_key            text NOT NULL,

  checksum              text NOT NULL,
  compressed_size_bytes bigint NOT NULL CHECK (compressed_size_bytes >= 0),
  source_record_count   integer NOT NULL CHECK (source_record_count >= 0),
  accepted_count        integer CHECK (accepted_count IS NULL OR accepted_count >= 0),
  rejected_count        integer CHECK (rejected_count IS NULL OR rejected_count >= 0),

  request_cursor        text,
  response_cursor       text,
  next_page_cursor      text,

  retrieved_at          timestamptz,
  verified_at           timestamptz,

  status                text NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending', 'verified', 'loaded', 'failed')),

  UNIQUE (run_id, page_number),
  UNIQUE (object_key)
);

CREATE INDEX IF NOT EXISTS ingestion_page_run_idx
  ON :"ops_schema".ingestion_page (run_id);

-- The sole canonical cursor source for the object-storage-backed
-- workflow (see module comment above). `lock_version` is optimistic-
-- concurrency metadata: object_raw_loader.py always
-- `SELECT ... FOR UPDATE` this row (or, for a not-yet-existing endpoint,
-- takes a session-level `pg_advisory_xact_lock` keyed on
-- (vendor, endpoint) -- see state.lock_cursor_for_update) before
-- validating and updating it, and always increments `lock_version` on
-- every successful commit -- so two runs racing to advance the same
-- endpoint's cursor can never silently clobber each other; the loser
-- blocks on the row lock, then re-validates the (now-newer) committed
-- cursor before deciding whether its own candidate is still safe to
-- apply (see docs/SOURCE_CONTRACT.md "Cursor safety").
CREATE TABLE IF NOT EXISTS :"ops_schema".ingestion_cursor (
  vendor                text NOT NULL,
  endpoint              text NOT NULL,
  committed_cursor      text,
  successful_run_id     uuid REFERENCES :"ops_schema".ingestion_run (run_id),
  committed_at          timestamptz,
  lock_version          bigint NOT NULL DEFAULT 0,
  PRIMARY KEY (vendor, endpoint)
);

-- One row per rejected source record. PHI-bearing: `raw_object_key`
-- points back to the immutable, durable page object in object storage
-- (never a copy of the raw payload itself) so investigation/replay is
-- always possible without doubling this connector's own PHI storage
-- footprint inside PostgreSQL. Retry-safe: (run_id, page_number,
-- record_position) is unique, so re-loading the same run/page after a
-- retry never inserts a duplicate rejected_record row (the loader uses
-- `ON CONFLICT (run_id, page_number, record_position) DO NOTHING`, the
-- same idempotency shape as the raw-row merge itself).
CREATE TABLE IF NOT EXISTS :"ops_schema".rejected_record (
  id                    bigserial PRIMARY KEY,
  run_id                uuid NOT NULL REFERENCES :"ops_schema".ingestion_run (run_id),
  page_number           integer NOT NULL,
  record_position       integer NOT NULL CHECK (record_position >= 1),

  reason_code           text NOT NULL,
  detail                text,

  source_record_id      text,
  payload_hash          text,
  raw_object_key        text NOT NULL,

  rejected_at           timestamptz NOT NULL DEFAULT now(),

  UNIQUE (run_id, page_number, record_position)
);

CREATE INDEX IF NOT EXISTS rejected_record_payload_hash_idx
  ON :"ops_schema".rejected_record (payload_hash)
  WHERE payload_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS rejected_record_reason_idx
  ON :"ops_schema".rejected_record (reason_code, rejected_at DESC);

-- Idempotent, PHI-free schema-drift observation (see
-- schema_observation.py). One row per distinct (vendor, endpoint,
-- field_path, observed_type) combination ever seen -- never one row per
-- run/page (that would defeat "upsert observations and occurrence
-- counts idempotently"); the run/page identity of the first and most
-- recent occurrence is retained instead, satisfying "vendor/source,
-- endpoint, run, and page identity" without re-inserting a row per
-- occurrence.
CREATE TABLE IF NOT EXISTS :"ops_schema".schema_observation (
  id                        bigserial PRIMARY KEY,
  vendor                    text NOT NULL,
  endpoint                  text NOT NULL,
  field_path                text NOT NULL,
  observed_type             text NOT NULL,

  fingerprint               text NOT NULL,

  first_observed_run_id     uuid REFERENCES :"ops_schema".ingestion_run (run_id),
  first_observed_page_number integer,
  first_observed_at         timestamptz NOT NULL DEFAULT now(),

  last_observed_run_id      uuid REFERENCES :"ops_schema".ingestion_run (run_id),
  last_observed_page_number integer,
  last_observed_at          timestamptz NOT NULL DEFAULT now(),

  occurrence_count          bigint NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),

  UNIQUE (vendor, endpoint, field_path, observed_type)
);

CREATE INDEX IF NOT EXISTS schema_observation_fingerprint_idx
  ON :"ops_schema".schema_observation (vendor, endpoint, fingerprint);

CREATE INDEX IF NOT EXISTS schema_observation_last_observed_idx
  ON :"ops_schema".schema_observation (vendor, endpoint, last_observed_at DESC);

-- --- 3. Raw-table metadata columns + source-stable uniqueness -----------

ALTER TABLE :"raw_schema".eligibility
  ADD COLUMN IF NOT EXISTS _ingestion_run_id  uuid,
  ADD COLUMN IF NOT EXISTS _ingested_at       timestamptz,
  ADD COLUMN IF NOT EXISTS _source_endpoint   text,
  ADD COLUMN IF NOT EXISTS _source_record_id  text,
  ADD COLUMN IF NOT EXISTS _source_updated_at timestamptz,
  ADD COLUMN IF NOT EXISTS _payload_hash      text,
  ADD COLUMN IF NOT EXISTS _raw_payload       jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS eligibility_source_stable_uk
  ON :"raw_schema".eligibility (_source_endpoint, _source_record_id, _source_updated_at, _payload_hash)
  WHERE _source_record_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS eligibility_ingestion_run_idx
  ON :"raw_schema".eligibility (_ingestion_run_id)
  WHERE _ingestion_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS eligibility_payload_hash_idx
  ON :"raw_schema".eligibility (_payload_hash)
  WHERE _payload_hash IS NOT NULL;

ALTER TABLE :"raw_schema".medical_claim
  ADD COLUMN IF NOT EXISTS _ingestion_run_id  uuid,
  ADD COLUMN IF NOT EXISTS _ingested_at       timestamptz,
  ADD COLUMN IF NOT EXISTS _source_endpoint   text,
  ADD COLUMN IF NOT EXISTS _source_record_id  text,
  ADD COLUMN IF NOT EXISTS _source_updated_at timestamptz,
  ADD COLUMN IF NOT EXISTS _payload_hash      text,
  ADD COLUMN IF NOT EXISTS _raw_payload       jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS medical_claim_source_stable_uk
  ON :"raw_schema".medical_claim (_source_endpoint, _source_record_id, _source_updated_at, _payload_hash)
  WHERE _source_record_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS medical_claim_ingestion_run_idx
  ON :"raw_schema".medical_claim (_ingestion_run_id)
  WHERE _ingestion_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS medical_claim_payload_hash_idx
  ON :"raw_schema".medical_claim (_payload_hash)
  WHERE _payload_hash IS NOT NULL;

ALTER TABLE :"raw_schema".pharmacy_claim
  ADD COLUMN IF NOT EXISTS _ingestion_run_id  uuid,
  ADD COLUMN IF NOT EXISTS _ingested_at       timestamptz,
  ADD COLUMN IF NOT EXISTS _source_endpoint   text,
  ADD COLUMN IF NOT EXISTS _source_record_id  text,
  ADD COLUMN IF NOT EXISTS _source_updated_at timestamptz,
  ADD COLUMN IF NOT EXISTS _payload_hash      text,
  ADD COLUMN IF NOT EXISTS _raw_payload       jsonb;

CREATE UNIQUE INDEX IF NOT EXISTS pharmacy_claim_source_stable_uk
  ON :"raw_schema".pharmacy_claim (_source_endpoint, _source_record_id, _source_updated_at, _payload_hash)
  WHERE _source_record_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS pharmacy_claim_ingestion_run_idx
  ON :"raw_schema".pharmacy_claim (_ingestion_run_id)
  WHERE _ingestion_run_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS pharmacy_claim_payload_hash_idx
  ON :"raw_schema".pharmacy_claim (_payload_hash)
  WHERE _payload_hash IS NOT NULL;

-- --- 4. Least-privilege grants --------------------------------------------

GRANT SELECT, INSERT, UPDATE ON
  :"ops_schema".ingestion_run,
  :"ops_schema".ingestion_page,
  :"ops_schema".ingestion_cursor,
  :"ops_schema".rejected_record,
  :"ops_schema".schema_observation
  TO :"ingest_role";

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA :"ops_schema" TO :"ingest_role";

REVOKE ALL ON :"ops_schema".rejected_record FROM PUBLIC;
REVOKE ALL ON
  :"ops_schema".ingestion_run,
  :"ops_schema".ingestion_page,
  :"ops_schema".ingestion_cursor,
  :"ops_schema".rejected_record,
  :"ops_schema".schema_observation
  FROM :"transform_role";

REVOKE ALL ON :"raw_schema".eligibility, :"raw_schema".medical_claim, :"raw_schema".pharmacy_claim FROM PUBLIC;
