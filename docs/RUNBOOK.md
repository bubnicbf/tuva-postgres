# Runbook

Day-to-day operational reference for the `tuva-ingest` connector. See
`README.md` for architecture and setup; `docs/API_MANIFEST.md` for the
manifest contract `extract` consumes.

## Routine operation

Endpoint-scoped, one endpoint at a time (the current, recommended way to
operate this connector -- see README.md's "extract / load / sync"
section for the full JSON output/run-id/retry contract):

```bash
uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
# {"event": "extract", "run_id": "019...", ...}
uv run tuva-ingest load --run-id 019...
# or, in one command:
uv run tuva-ingest sync --endpoint medical-claims --since 2025-01-01
```

Repeat for each endpoint (`medical-claims`, `pharmacy-claims`,
`eligibility`) your operational schedule needs -- each is independent;
loading one never truncates or touches another endpoint's raw table.

Legacy full pipeline (all three tables, one manifest request), one
command (requires `.env` populated -- see `scripts/setup_env.example`;
see README.md's "Backward compatibility" section):

```bash
make run   # migrate -> extract -> load-raw -> dbt deps ->
           # dbt build --select tag:input_layer -> dbt build --select tag:dq_structural
```

`tuva-ingest run` (what `make run` invokes) stops at the first failed
stage -- it never attempts `dbt build --select tag:dq_structural` after
`tag:input_layer` fails, and never marks the run `succeeded` unless both
pass. See README.md's "Validation order" for why this gate exists and
how the same order is enforced in `make pipeline` and CI.

Or step by step, for more control/visibility between stages:

```bash
make migrate           # apply pending operational migrations (idempotent)
make extract           # fetch + publish a raw snapshot (all 3 tables, legacy full manifest)
make load-raw          # load the current published snapshot into RAW_SCHEMA
make dbt-debug          # connection/profile sanity check
make dbt-deps            # fetch the pinned Tuva 0.18.0 package (needs network)
make dbt-parse            # Jinja/YAML/ref() validation, no database needed
make dbt-input-layer       # this connector's own staging + Input Layer models + tests
make dbt-dq-structural      # the pinned Tuva package's structural DQ (must pass before anything below)
make dbt-build                # (equivalent to the old unconditional "build everything")
make health                    # DB connectivity + migration state + freshness
```

Or the whole stop-on-first-failure validation order in one command:

```bash
make pipeline   # quality -> dbt-debug -> dbt-deps -> dbt-parse -> dbt-input-layer -> dbt-dq-structural
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
- **`dbt deps` failed**: almost always a network-access problem
  reaching dbt Hub -- fix connectivity and rerun `make dbt-deps`.
- **`dbt build --select tag:input_layer` failed**: a bug in this
  connector's own staging/final models or their schema tests. Fix it,
  then rerun `make dbt-input-layer` (or `make dbt-dq-structural`
  afterward, since a failure here means that stage never ran).
- **`dbt build --select tag:dq_structural` failed**: the pinned Tuva
  package's structural DQ found a problem with the Input Layer models'
  shape (missing model/column, wrong type, broken key). See README.md's
  "Validation order" -- fix `models/final/*.sql`/`schema.yml` and rerun
  `make dbt-input-layer` then `make dbt-dq-structural` (never skip
  straight to logical/analytical DQ or `make run` again until this
  passes).
- None of the `dbt-*` targets mutate the raw schema, so any of the
  above failures never requires re-running `extract`/`load-raw`.
- **Migration checksum mismatch**: a previously applied migration file
  changed on disk. Migrations are immutable once applied -- revert the
  change and add a new migration instead of editing an applied one.

### Other common failure modes

- **"could not find model" / package resolution errors**: usually means
  `dbt deps` was not (re)run after a `packages.yml` change, or
  `flags.require_ref_searches_node_package_before_root` was removed
  from `dbt_project.yml` -- the pinned Tuva package needs that flag to
  `ref()` this project's `models/final/*.sql` by name.
- **Unexpected schema name**: see README.md's "Actual generated
  PostgreSQL schema names" -- confirm `macros/generate_schema_name.sql`
  is still present and unmodified, and that `INPUT_LAYER_SCHEMA`/
  `input_layer_schema` is set to what you expect.
- **Missing column / type mismatch reported by `dbt build --select
  tag:dq_structural`**: `models/final/*.sql` has drifted from the
  pinned package's actual contract. Re-derive the column list from an
  official Tuva source for the pinned version (never from this
  repository's own prior SQL alone -- see README.md "Upgrading beyond
  Tuva 0.18.0") and update the model + `models/final/schema.yml`
  together.
- **`tag:dq_structural` selects zero nodes**: the tag name this
  connector assumes does not match the installed package version. Run
  `dbt ls --select tag:dq_structural` after `dbt deps` to find the
  correct selector for your pinned version and update
  `Makefile`/`README.md`/`.github/workflows/ci.yml` together (see
  README.md "Known limitations").

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
