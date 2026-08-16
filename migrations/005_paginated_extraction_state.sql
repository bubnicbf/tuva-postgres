-- migrations/005_paginated_extraction_state.sql
--
-- Forward-only addition supporting the paginated JSON extraction
-- contract (see src/tuva_ingest/pagination.py, paginated_loader.py,
-- docs/SOURCE_CONTRACT.md "Pagination"). Never rewrites 001-004 --
-- migrations are immutable once applied.
--
-- Three additions, all in either :"ops_schema" or :"raw_schema" (never
-- Tuva-managed):
--
-- 1. :"ops_schema".source_watermarks -- the durable high-water-mark
--    record this connector's transactional load/reconcile/commit step
--    (paginated_loader.py, cli.py's `_run_paginated_load`) advances only
--    after a full extract+load+reconcile succeeds. Keyed by
--    (source, endpoint) -- one row per endpoint per source, since each
--    endpoint's pagination/watermark progresses independently.
--
-- 2. Five new nullable columns on each of the three raw landing tables
--    (:"raw_schema".eligibility/medical_claim/pharmacy_claim) --
--    endpoint, page_number, source_page_token, retrieved_at,
--    file_sha256 -- the additional per-row ingestion metadata the
--    paginated contract records (see paginated_loader.py) that the
--    legacy CSV/manifest contract's four original columns
--    (_snapshot_id, _source_row_number, _loaded_at, raw_row --
--    migrations/002_ingestion_control.sql) do not carry. Nullable
--    because every row loaded by the legacy `load-raw`/`run` commands
--    leaves them NULL, which is a valid, permanent state, not a
--    "pending migration" for old rows -- exactly the same pattern
--    migrations/004_endpoint_scoped_ingestion.sql already established
--    for `ingestion_runs.endpoint`/`requested_since`.
--
--    `_snapshot_id` is reused (not duplicated into a new `run_id`
--    column) to store the paginated run's run_id, and
--    `_source_row_number` is reused (not duplicated into a new
--    `source_record_number` column) to store a global, run-wide running
--    record position -- both already exist, already mean almost exactly
--    what the paginated contract needs, and reusing them keeps a single
--    raw table serving both contracts without a parallel schema.
--
-- 3. A unique index on (_snapshot_id, _source_row_number) for each raw
--    table -- backs the idempotent `INSERT ... ON CONFLICT
--    (_snapshot_id, _source_row_number) DO NOTHING` the paginated loader
--    uses (paginated_loader.load_paginated_run) so repeating a completed
--    load never duplicates rows. Safe to add alongside the legacy
--    TRUNCATE+COPY loader (raw_loader.py): that loader TRUNCATEs before
--    every COPY, so no two rows in one legacy load can ever share a
--    (_snapshot_id, _source_row_number) pair in the first place.
--
-- Idempotent: every statement uses IF NOT EXISTS; rerunning this
-- migration against an already-migrated database is a safe no-op.

-- --- 1. Durable high-water-mark state -----------------------------------

CREATE TABLE IF NOT EXISTS :"ops_schema".source_watermarks (
  source              text NOT NULL,
  endpoint            text NOT NULL,
  high_water_mark     text,
  successful_run_id   text,
  committed_at        timestamptz,
  PRIMARY KEY (source, endpoint)
);

-- --- 2 & 3. Raw table metadata columns + idempotency indexes ------------

ALTER TABLE :"raw_schema".eligibility
  ADD COLUMN IF NOT EXISTS endpoint text,
  ADD COLUMN IF NOT EXISTS page_number integer,
  ADD COLUMN IF NOT EXISTS source_page_token text,
  ADD COLUMN IF NOT EXISTS retrieved_at timestamptz,
  ADD COLUMN IF NOT EXISTS file_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS eligibility_snapshot_row_key
  ON :"raw_schema".eligibility (_snapshot_id, _source_row_number);

ALTER TABLE :"raw_schema".medical_claim
  ADD COLUMN IF NOT EXISTS endpoint text,
  ADD COLUMN IF NOT EXISTS page_number integer,
  ADD COLUMN IF NOT EXISTS source_page_token text,
  ADD COLUMN IF NOT EXISTS retrieved_at timestamptz,
  ADD COLUMN IF NOT EXISTS file_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS medical_claim_snapshot_row_key
  ON :"raw_schema".medical_claim (_snapshot_id, _source_row_number);

ALTER TABLE :"raw_schema".pharmacy_claim
  ADD COLUMN IF NOT EXISTS endpoint text,
  ADD COLUMN IF NOT EXISTS page_number integer,
  ADD COLUMN IF NOT EXISTS source_page_token text,
  ADD COLUMN IF NOT EXISTS retrieved_at timestamptz,
  ADD COLUMN IF NOT EXISTS file_sha256 text;

CREATE UNIQUE INDEX IF NOT EXISTS pharmacy_claim_snapshot_row_key
  ON :"raw_schema".pharmacy_claim (_snapshot_id, _source_row_number);
