-- db/migrations/sql/0002_operational_schema.sql
-- Operational tables (pipeline_runs, pipeline_artifacts) in a dedicated,
-- configurable operations schema (:"ops_schema"), separate from the core
-- data schema. Never stores API tokens, PG_DSN, CSV row data, or
-- authorization headers -- only run/artifact metadata.
--
-- The schema_migrations table itself is intentionally NOT created here:
-- it must exist before any migration (including this one) can be
-- tracked, so the migration runner bootstraps it directly (see
-- src/tuva_postgres/migrations.py). This file only owns the tables that
-- are meaningfully "migration-managed" from the pipeline's first tracked
-- change onward.

CREATE SCHEMA IF NOT EXISTS :"ops_schema";

CREATE TABLE IF NOT EXISTS :"ops_schema".pipeline_runs (
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

  artifact_count    integer,
  bytes_downloaded  bigint,
  rows_loaded       jsonb,       -- {"patient": 1000, "encounter": 2500, ...}
  tests_passed      integer,
  tests_failed      integer
);

CREATE INDEX IF NOT EXISTS pipeline_runs_started_at_idx
  ON :"ops_schema".pipeline_runs (started_at DESC);

CREATE INDEX IF NOT EXISTS pipeline_runs_succeeded_idx
  ON :"ops_schema".pipeline_runs (finished_at DESC)
  WHERE status = 'succeeded';

CREATE INDEX IF NOT EXISTS pipeline_runs_snapshot_idx
  ON :"ops_schema".pipeline_runs (source, snapshot_id);

CREATE INDEX IF NOT EXISTS pipeline_runs_failed_idx
  ON :"ops_schema".pipeline_runs (started_at DESC)
  WHERE status = 'failed';

CREATE TABLE IF NOT EXISTS :"ops_schema".pipeline_artifacts (
  id                    bigserial PRIMARY KEY,
  run_id                text NOT NULL REFERENCES :"ops_schema".pipeline_runs (run_id) DEFERRABLE INITIALLY DEFERRED,
  table_name            text NOT NULL,

  source_url            text NOT NULL,   -- credential-free: auth travels only via the Authorization header, never in the URL
  expected_sha256       text NOT NULL,
  actual_sha256         text,
  expected_size_bytes   bigint NOT NULL,
  actual_size_bytes     bigint,

  raw_path              text,
  download_status       text NOT NULL DEFAULT 'pending' CHECK (download_status IN ('pending', 'ok', 'failed')),
  load_status            text NOT NULL DEFAULT 'pending' CHECK (load_status IN ('pending', 'ok', 'failed')),

  created_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS pipeline_artifacts_run_idx
  ON :"ops_schema".pipeline_artifacts (run_id);
