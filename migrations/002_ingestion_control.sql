-- migrations/002_ingestion_control.sql
--
-- Two groups of objects, both owned entirely by this connector (never
-- Tuva-managed):
--
-- 1. Operational control objects in :"ops_schema" -- ingestion_runs
--    (one row per `tuva-ingest run`/`load-raw` invocation) and
--    table_loads (one row per managed raw table per run). See
--    src/tuva_ingest/state.py for the only code that writes to these.
--    Never stores API tokens, PG_DSN, CSV row data, or authorization
--    headers -- only run/table-load metadata.
--
-- 2. Raw landing tables in :"raw_schema" -- eligibility, medical_claim,
--    pharmacy_claim (see src/tuva_ingest/manifest.RAW_TABLES and
--    models/sources.yml). Each stores every row as a single `raw_row
--    jsonb` column, built directly from that CSV's own header and
--    values with no type coercion at load time (see
--    src/tuva_ingest/raw_loader.py's module docstring for why) --
--    Tuva-specific typing/normalization happens in dbt staging models
--    (models/staging/), not here. `_snapshot_id` scopes every row to the
--    extraction snapshot it came from; a raw table always holds exactly
--    one snapshot's worth of data at a time (see raw_loader.py's
--    TRUNCATE-then-COPY retry semantics).
--
-- Idempotent: every statement uses IF NOT EXISTS; rerunning this
-- migration against an already-migrated database is a safe no-op.

-- --- 1. Operational control -------------------------------------------

CREATE TABLE IF NOT EXISTS :"ops_schema".ingestion_runs (
  run_id            text PRIMARY KEY,
  source            text NOT NULL,
  snapshot_id       text,
  environment       text NOT NULL,
  app_version       text NOT NULL,
  host              text,

  started_at        timestamptz NOT NULL DEFAULT now(),
  finished_at       timestamptz,

  status            text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'skipped')),
  current_stage     text,

  error_category    text,
  error_message     text,

  rows_loaded       jsonb,       -- {"eligibility": 1000, "medical_claim": 5000, ...}
  tables_loaded     text[]
);

CREATE INDEX IF NOT EXISTS ingestion_runs_started_at_idx
  ON :"ops_schema".ingestion_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS ingestion_runs_succeeded_idx
  ON :"ops_schema".ingestion_runs (finished_at DESC)
  WHERE status = 'succeeded';

CREATE INDEX IF NOT EXISTS ingestion_runs_snapshot_idx
  ON :"ops_schema".ingestion_runs (source, snapshot_id);

CREATE INDEX IF NOT EXISTS ingestion_runs_failed_idx
  ON :"ops_schema".ingestion_runs (started_at DESC)
  WHERE status = 'failed';

CREATE TABLE IF NOT EXISTS :"ops_schema".table_loads (
  id                    bigserial PRIMARY KEY,
  run_id                text NOT NULL REFERENCES :"ops_schema".ingestion_runs (run_id) DEFERRABLE INITIALLY DEFERRED,
  table_name            text NOT NULL,

  expected_sha256       text NOT NULL,
  actual_sha256         text,
  expected_size_bytes   bigint NOT NULL,
  actual_size_bytes     bigint,

  row_count             bigint,
  load_status           text NOT NULL DEFAULT 'pending' CHECK (load_status IN ('pending', 'succeeded', 'failed')),
  error_message         text,

  started_at            timestamptz NOT NULL DEFAULT now(),
  finished_at           timestamptz
);

CREATE INDEX IF NOT EXISTS table_loads_run_idx
  ON :"ops_schema".table_loads (run_id);

-- --- 2. Raw landing tables ----------------------------------------------

CREATE TABLE IF NOT EXISTS :"raw_schema".eligibility (
  _snapshot_id        text NOT NULL,
  _source_row_number  bigint NOT NULL,
  _loaded_at          timestamptz NOT NULL,
  raw_row             jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS eligibility_snapshot_idx
  ON :"raw_schema".eligibility (_snapshot_id);

CREATE TABLE IF NOT EXISTS :"raw_schema".medical_claim (
  _snapshot_id        text NOT NULL,
  _source_row_number  bigint NOT NULL,
  _loaded_at          timestamptz NOT NULL,
  raw_row             jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS medical_claim_snapshot_idx
  ON :"raw_schema".medical_claim (_snapshot_id);

CREATE TABLE IF NOT EXISTS :"raw_schema".pharmacy_claim (
  _snapshot_id        text NOT NULL,
  _source_row_number  bigint NOT NULL,
  _loaded_at          timestamptz NOT NULL,
  raw_row             jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS pharmacy_claim_snapshot_idx
  ON :"raw_schema".pharmacy_claim (_snapshot_id);
