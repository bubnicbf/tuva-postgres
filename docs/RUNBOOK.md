# Runbook

Day-to-day operational reference for the `tuva-ingest` connector. See
`README.md` for architecture and setup; `docs/API_MANIFEST.md` for the
manifest contract `extract` consumes.

## Routine operation

Full pipeline, one command (requires `.env` populated -- see
`scripts/setup_env.example`):

```bash
make run   # migrate -> extract -> load-raw -> dbt deps -> dbt build
```

Or step by step, for more control/visibility between stages:

```bash
make migrate          # apply pending operational migrations (idempotent)
make extract          # fetch + publish a raw snapshot
make load-raw          # load the current published snapshot into RAW_SCHEMA
make dbt-deps           # fetch the pinned Tuva 0.18.0 package (needs network)
make dbt-build           # staging -> Input Layer -> Tuva's own models + tests
make health              # DB connectivity + migration state + freshness
```

Every command reads its configuration from `.env` (or the process
environment directly, e.g. in CI/containers) via
`src/tuva_ingest/config.py`.

## Checking status

```bash
make migrate-status   # read-only: applied/pending migrations, checksum drift
make health            # db_connect / migrations / freshness, exit 0 if healthy
```

`tuva-ingest healthcheck` never mutates anything and never prints
`PG_DSN` or any secret -- safe to run from a container health probe.

## Recovering from a failed run

Every run's stage, status, and error are recorded in
`ingest_ops.ingestion_runs`/`table_loads` (see
`migrations/002_ingestion_control.sql`). To inspect the most recent run:

```sql
SELECT run_id, status, current_stage, error_category, error_message, started_at, finished_at
FROM ingest_ops.ingestion_runs
ORDER BY started_at DESC
LIMIT 5;
```

Recovery is always "run the same command again" -- every stage is
designed to be retry-safe:

- **`extract` failed partway through a download**: the partially
  staged snapshot was already cleaned up (see `extract.py`'s
  `RawSnapshotStore.abort_staging`); nothing was published. Just rerun
  `make extract`.
- **`load-raw` failed partway through (bad checksum, connection
  drop)**: the whole transaction (all three raw tables + run
  bookkeeping) rolled back together; no partial snapshot is visible.
  Rerun `make load-raw` -- the same snapshot_id reloads cleanly (raw
  tables are `TRUNCATE`d and reloaded per snapshot, never appended to).
- **`dbt deps`/`dbt build` failed**: fix the underlying issue (network
  access for `dbt deps`; a failing model/test for `dbt build`) and
  rerun `make dbt-deps`/`make dbt-build`. Neither mutates the raw
  schema, so this never requires re-running `extract`/`load-raw`.
- **Migration checksum mismatch**: a previously applied migration file
  changed on disk. Migrations are immutable once applied -- revert the
  change and add a new migration instead of editing an applied one.

## Retention and reruns

- Raw snapshots under `RAW_DATA_DIR` are never deleted automatically --
  prune old ones manually once you no longer need to replay them.
- Reloading an already-loaded `snapshot_id` is always safe (no
  duplication); reloading a *different* `snapshot_id` replaces the raw
  tables' contents entirely (each raw table only ever holds the most
  recently loaded snapshot).
- `dbt build` is always safe to rerun -- staging models are views,
  final Input Layer models are tables rebuilt from the current raw
  contents each run.

## Local disposable PostgreSQL

```bash
make local-db-ready     # start postgres (docker compose), wait healthy, migrate
make local-db-status    # container state + migration status
make local-db-shell     # psql against it
make local-db-logs      # follow postgres logs
make local-db-down      # stop containers, KEEP data
make local-db-reset     # DESTRUCTIVE: drop the data volume too (asks for confirmation)
```

## Running the test suite

```bash
make test-unit          # database-free
make test-integration   # requires PG_DSN pointed at a DISPOSABLE database -- never production
make quality             # full database-free quality gate (lock check, lint, types, unit tests, sql lint)
make ci-full              # quality + dbt parse/deps + the full integration suite
```

`tests/integration/` creates its own uniquely-suffixed
`raw_test_<suffix>`/`ops_test_<suffix>` schemas and drops them (plus
their throwaway roles) on teardown -- it never touches `raw`,
`ingest_ops`, `input_layer`, or any name a real deployment would use.

## Upgrading the pinned Tuva package version

1. Update the version pin in `packages.yml`.
2. `make dbt-deps` to fetch it.
3. Review Tuva's own release notes for Input Layer contract changes
   between the two versions.
4. Update `models/final/*.sql`/`models/final/schema.yml` for any
   changed column names, types, or new required fields.
5. `make dbt-build` against a disposable database with representative
   fixtures before deploying.

Never float a version range or point at `main`/`latest` -- every
upgrade is a deliberate, single-commit, reviewed change.

## Security notes

- `.env` and `profiles.yml` are git-ignored; never commit either.
- `PG_DSN`/`TUVA_API_TOKEN` are redacted from every log line, error
  message, and `IngestConfig.safe_dict()`/`repr()` (see
  `src/tuva_ingest/logging_utils.py`).
- Rotate `TUVA_API_TOKEN` by updating `.env`/your secret store -- no
  code change is required.
- This repository's own test fixtures (`tests/fixtures/*.csv`) are
  synthetic and contain no PHI; do not commit real extracted snapshots
  or database dumps to this repository.

## What this repository does not own

This repository does not define, migrate, or reproduce any Tuva-managed
core, terminology, or output table. If you need to inspect Tuva's own
schema, look at `dbt_packages/the_tuva_project/` after running `dbt
deps` -- never add equivalent DDL here.
