-- migrations/004_endpoint_scoped_ingestion.sql
--
-- Forward-only addition supporting the endpoint-scoped `tuva-ingest
-- extract --endpoint ... --since ...` / `load --run-id ...` / `sync`
-- commands (see src/tuva_ingest/cli.py). Never rewrites 001-003 --
-- migrations are immutable once applied (see
-- src/tuva_ingest/migrations.py's module docstring).
--
-- Two additions, both in :"ops_schema" (never Tuva-managed, never the
-- raw schema):
--
-- 1. `ingestion_runs.endpoint` / `ingestion_runs.requested_since` --
--    records the operator-facing endpoint name (`medical-claims`,
--    `pharmacy-claims`, `eligibility` -- see
--    src/tuva_ingest/endpoints.py) and the `--since` value requested for
--    this run, for operator auditing. Nullable: legacy `run`/`load-raw`
--    runs (the full, all-three-tables-in-one-manifest flow) never set
--    either column, and that is a valid, permanent state, not a
--    "pending migration" for old rows.
--
-- 2. A unique index on `table_loads (run_id, table_name)` -- this
--    connector reuses the extraction's own immutable `snapshot_id` as
--    the run id for `extract`/`load`/`sync` (see
--    src/tuva_ingest/extract.EndpointExtractResult), so
--    `load --run-id X` must be safe to repeat for the exact same
--    run_id. Without this, `state.upsert_table_load_pending` (see
--    src/tuva_ingest/state.py) would need a plain `INSERT` instead of
--    `INSERT ... ON CONFLICT (run_id, table_name) DO UPDATE`, and a
--    second `load --run-id X` would accumulate a second, duplicate
--    `table_loads` row rather than idempotently describing the same
--    (run_id, table) load. `ON CONFLICT` can target any unique index
--    covering exactly those columns -- it does not need to be a
--    separately named table CONSTRAINT -- so a plain
--    `CREATE UNIQUE INDEX IF NOT EXISTS` is enough here and keeps this
--    migration's idempotency the same simple, declarative
--    `IF NOT EXISTS` style as every other statement in migrations/001-003
--    (unlike a `CONSTRAINT`, PostgreSQL has no
--    `ADD CONSTRAINT IF NOT EXISTS` form, which would otherwise force a
--    `DO $$ ... $$` existence-check block here instead).
--
-- Idempotent: every statement uses IF NOT EXISTS; rerunning this
-- migration against an already-migrated database is a safe no-op.

ALTER TABLE :"ops_schema".ingestion_runs
  ADD COLUMN IF NOT EXISTS endpoint text;

ALTER TABLE :"ops_schema".ingestion_runs
  ADD COLUMN IF NOT EXISTS requested_since text;

CREATE INDEX IF NOT EXISTS ingestion_runs_endpoint_idx
  ON :"ops_schema".ingestion_runs (endpoint, started_at DESC)
  WHERE endpoint IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS table_loads_run_id_table_name_key
  ON :"ops_schema".table_loads (run_id, table_name);
