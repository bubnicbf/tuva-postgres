# tuva-ingest

A raw-to-Input-Layer ingestion connector for [the Tuva Project](https://thetuvaproject.com)
dbt package. `tuva-ingest` extracts source claims data through a
versioned manifest contract, loads it into a configurable raw
PostgreSQL warehouse schema, and hands it off to dbt to map into the
Tuva Input Layer -- the pinned Tuva package (`tuva-health/the_tuva_project`,
exactly `0.18.0`) then builds its own core data model, terminology sets,
and marts on top of that.

This repository does not own or reproduce any of Tuva's core,
terminology, or output DDL. Every table this repository defines lives
in the raw landing schema or the operational/control schema; Tuva's own
tables are created and owned entirely by the pinned dbt package.

## Architecture

```
   API manifest              raw PostgreSQL schema        dbt: staging          dbt: Input Layer         Tuva package (0.18.0)
  (versioned JSON)     -->    (JSONB schema-on-read)  -->  (typed, trimmed) -->  (eligibility,       -->  (core, terminology,
                                                                                  medical_claim,           marts -- never
  src/tuva_ingest/            src/tuva_ingest/             models/staging/       pharmacy_claim)          duplicated locally)
  extract.py, api_client.py   raw_loader.py                                      models/final/
```

1. **Extract** (`tuva-ingest extract`) -- fetches and validates a
   versioned JSON manifest (see `docs/API_MANIFEST.md`) describing one
   snapshot's per-table CSV artifacts, downloads each one
   (checksum-verified, retried, bearer-authenticated), and publishes
   the snapshot atomically under `RAW_DATA_DIR`. A partial download can
   never appear complete to a later step.
2. **Load raw** (`tuva-ingest load-raw`) -- loads the published
   snapshot into the configured raw schema (`RAW_SCHEMA`, default
   `raw`) only. Every row is stored as a single `raw_row jsonb` column
   plus fixed metadata columns (`_snapshot_id`, `_source_row_number`,
   `_loaded_at`) -- no type coercion, renaming, or business logic
   happens here, so the raw layer is a faithful, replayable copy of
   exactly what the source sent.
3. **dbt** (`tuva-ingest dbt -- <args>`) -- `models/staging/*.sql`
   types and normalizes the raw JSONB into typed columns;
   `models/final/{eligibility,medical_claim,pharmacy_claim}.sql`
   expose the Tuva Input Layer contract those staging models feed. dbt
   never writes back into the raw schema.
4. **Tuva package** -- pinned to exactly `0.18.0`
   (`packages.yml`), `ref()`s this project's Input Layer models by
   name and builds its own core/terminology/mart models on top of
   them, in its own schema(s). This repository never vendors or
   duplicates any of that package's model files.

Source data is never loaded directly into any Tuva-managed schema.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12, pinned via
  `.python-version`)
- Docker + Docker Compose v2, for a local disposable PostgreSQL (or
  point `PG_DSN` at any PostgreSQL 16+ instance you already have)
- Network access to dbt Hub (for `dbt deps`, which fetches the pinned
  Tuva package) when you actually run dbt

## Quick start

```bash
git clone <this repo> && cd tuva-postgres
make init                      # uv sync --locked; installs pre-commit; copies .env template
# edit .env: at minimum PG_DSN, and TUVA_API_MANIFEST_URL/TUVA_API_TOKEN
# if you plan to run `extract`/`run` against a real manifest endpoint

make local-db-ready            # starts a local disposable Postgres (docker compose) and migrates it
make migrate-status            # read-only: confirm 001-003 are applied

make extract                   # fetch + publish a raw snapshot (requires TUVA_API_MANIFEST_URL/TOKEN)
make load-raw                  # load the published snapshot into RAW_SCHEMA
make dbt-deps                  # fetch the pinned Tuva 0.18.0 package
make dbt-build                 # staging -> Input Layer -> Tuva's own models, plus dbt tests

make health                    # DB connectivity + migration state + last-successful-run freshness
```

Or run the whole pipeline in one command once `.env` is populated:

```bash
make run   # migrate -> extract -> load-raw -> dbt deps -> dbt build
```

## Configuration

Every setting is an environment variable, loaded and validated by
`src/tuva_ingest/config.py`'s `IngestConfig.load()` -- see
`scripts/setup_env.example` for the full list with safe local
defaults. `make init` copies it to `.env` (git-ignored) for you.

| Variable | Purpose | Default |
| --- | --- | --- |
| `PG_DSN` | PostgreSQL connection string | *(required)* |
| `RAW_SCHEMA` | Raw landing schema (never Tuva-managed) | `raw` |
| `OPS_SCHEMA` | Operational/control schema (run/table-load history) | `ingest_ops` |
| `INPUT_LAYER_SCHEMA` | Schema dbt materializes `models/final/*.sql` into | `input_layer` |
| `INGEST_ROLE` / `TRANSFORM_ROLE` | Least-privilege role names (see `migrations/003_roles_and_grants.sql`) | `tuva_ingest_role` / `tuva_transform_role` |
| `TUVA_API_MANIFEST_URL` / `TUVA_API_TOKEN` | Manifest endpoint + bearer token (see `docs/API_MANIFEST.md`) | *(required for `extract`/`run`)* |
| `TUVA_API_TIMEOUT_SECONDS` / `TUVA_API_MAX_RETRIES` | HTTP client bounds | `30` / `5` |
| `TUVA_API_ALLOW_INSECURE_HTTP` | Allow `http://` manifest/artifact URLs (local mock servers only) | `0` |
| `RAW_DATA_DIR` | Local extraction/snapshot directory | `data/raw` |
| `SOURCE_NAME` | Top-level directory name under `RAW_DATA_DIR` | `tuva` |
| `DBT_TARGET` / `DBT_PROFILES_DIR` / `DBT_PROJECT_DIR` | Passed through to every `dbt` invocation | `dev` / `.` / `.` |
| `PIPELINE_ENVIRONMENT` / `PIPELINE_MAX_SUCCESS_AGE_HOURS` | Healthcheck freshness window | `local` / `30` |
| `LOG_LEVEL` | Structured JSON log level | `INFO` |

`IngestConfig.load()` fails fast with every problem listed at once
(never just the first), validates every dynamic schema/role name
against a single shared identifier policy (`src/tuva_ingest/identifiers.py`)
before any SQL is composed, and never includes `PG_DSN`/`TUVA_API_TOKEN`
in `repr()`, logs, or `safe_dict()` output.

## Local PostgreSQL (Docker Compose)

`compose.yml` provides a disposable local Postgres plus one-shot
`migrate`/`dbt-deps`/`dbt-build` services and an `ingest` service for
the connector CLI. See the file's own header comment for the full
command reference. Routine lifecycle:

```bash
make local-db-ready     # start postgres, wait healthy, apply migrations
make local-db-status    # container state + migration status (read-only)
make local-db-shell     # psql against the local database
make local-db-down      # stop containers, KEEP the data volume
make local-db-reset     # DESTRUCTIVE: also drops the data volume
```

## Migrations

`migrations/001_operational_schemas.sql`, `002_ingestion_control.sql`,
and `003_roles_and_grants.sql` are the only DDL this repository owns:
the raw and operational-control schemas, run/table-load bookkeeping
tables, and least-privilege role grants. They are checksum-tracked
(`src/tuva_ingest/migrations.py`), applied at most once each, and
rerunning `tuva-ingest migrate` against an already-migrated database is
always a true no-op. Dynamic identifiers (schema/role names) use
psql-style `:"name"` substitution, validated against the same shared
identifier policy every other dynamic-SQL call site uses -- see
`migrations.py`'s module docstring for why static SQL alone can't
express this.

## dbt project

- `dbt_project.yml` -- claims-only Tuva var configuration
  (`claims_enabled: true`, `clinical_enabled`/`provider_attribution_enabled: false`)
  and the `require_ref_searches_node_package_before_root` flag Tuva
  0.18.0 requires.
- `packages.yml` -- `tuva-health/the_tuva_project` pinned to exactly
  `0.18.0`, plus `dbt_utils` (used by `models/final/schema.yml`'s
  uniqueness tests).
- `profiles.example.yml` -- entirely environment-variable-driven, with
  safe local placeholder defaults only; never a real credential. Copy
  to `profiles.yml` (git-ignored) or rely on the Docker image, which
  bakes this same file in as `profiles.yml` since it contains nothing
  secret.
- `models/sources.yml` -- declares the three raw tables
  (`eligibility`, `medical_claim`, `pharmacy_claim`) with freshness
  checks.
- `models/staging/*.sql` -- normalizes `raw_row` JSONB into typed,
  trimmed columns (empty string -> `NULL`; malformed dates/numerics ->
  typed `NULL` via the `safe_date`/`safe_numeric`/`safe_integer`
  macros in `macros/safe_cast.sql`, since PostgreSQL has no
  `TRY_CAST`). No Tuva-specific business logic here.
- `models/final/*.sql` -- the Input Layer contract models Tuva's
  package `ref()`s by name. Fields this connector's source data cannot
  confidently supply are typed `NULL`, documented in each model's own
  header comment and in `models/final/schema.yml` -- never an invented
  mapping.

## Testing

```bash
make test-unit          # database-free: fakes/mocks only, safe to run anywhere
make test-integration   # requires PG_DSN pointed at a DISPOSABLE PostgreSQL database
make quality            # dependency lock check, ruff, mypy, unit tests, sqlfluff -- database-free
make ci-full            # quality + dbt parse/deps + the full integration suite
```

`tests/unit/` fakes or mocks every I/O boundary (an in-process
`http.server` for the API client, fake psycopg connections for
SQL-composition tests) so it never touches a real database or network.
`tests/integration/test_pipeline_integration.py` requires a real,
disposable PostgreSQL database (never point it at production) and
proves: migrations are truly idempotent on a second run; reloading the
same snapshot never duplicates rows; a corrupted checksum rolls back
the *entire* snapshot's transaction, not just one table; ingestion
never creates or touches any Tuva-managed schema name; and (when `dbt`
is on `PATH`, which `uv sync --locked` guarantees) a real `dbt build`
against the fixtures in `tests/fixtures/` produces the three Input
Layer tables with the expected row counts.

## Failure recovery and idempotency

- **Extraction**: a snapshot is only "published" once every artifact
  is downloaded and verified and a `_SUCCESS` marker is written.
  Re-extracting the exact same manifest is a safe no-op; re-extracting
  a `snapshot_id` with *different* content is a loud failure, never a
  silent overwrite.
- **Raw loading**: each raw table is `TRUNCATE`d and reloaded from a
  specific `snapshot_id` inside one transaction spanning all three
  tables plus run bookkeeping -- retrying never duplicates rows, and a
  failure partway through never leaves a partially-loaded snapshot
  visible.
- **Migrations**: checksum-tracked and applied at most once;
  `apply_pending` takes a PostgreSQL advisory lock so concurrent runs
  never race.
- **Run state**: `ingest_ops.ingestion_runs`/`table_loads` (see
  `migrations/002_ingestion_control.sql`) record every run's stage,
  status, row counts, and errors. A run can only leave `running`
  exactly once (see `src/tuva_ingest/state.py`).

## Security

- Never commit `.env` or `profiles.yml` (both git-ignored).
- `PG_DSN` and `TUVA_API_TOKEN` are never logged, printed, or included
  in any error message (`src/tuva_ingest/logging_utils.py`'s
  `sanitize_error`/`sanitize_text`).
- Every dynamic schema/table/role identifier is validated against
  `src/tuva_ingest/identifiers.py`'s shared policy before it is ever
  composed into SQL text; data values are always bound through
  parameterized queries or psycopg's `COPY` protocol, never
  interpolated.
- This connector never handles PHI in its own test fixtures
  (`tests/fixtures/*.csv` are synthetic).

## Upgrading beyond Tuva 0.18.0

Bump the pin in `packages.yml`, re-run `dbt deps`, and review Tuva's
own changelog for Input Layer contract changes before touching
`models/final/*.sql`. Treat any upgrade as a deliberate, reviewed
change -- never a floating range or `main`/`latest`.

## Known limitations

The exact Tuva 0.18.0 Input Layer column contract for
`eligibility`/`medical_claim`/`pharmacy_claim` should be verified
against the pinned package's own source (`dbt_packages/the_tuva_project/`
after `dbt deps`) before relying on this connector's `models/final/*.sql`
in production -- the mapping here follows Tuva's documented conventions
and this repository's own claims data model, but every field this
connector's source cannot confidently supply is a typed `NULL` (see
each model's header comment), not a guess.

## Repository layout

```
src/tuva_ingest/     the connector: config, api_client, extract, raw_loader, state, migrations, cli
migrations/           001-003: raw + operational schemas, run/table-load control, role grants
dbt_project.yml, packages.yml, profiles.example.yml, macros/, models/   the dbt project
tests/unit/            database-free tests
tests/integration/     tests requiring a disposable PostgreSQL database
tests/fixtures/         small synthetic CSV fixtures used by tests/integration
docs/RUNBOOK.md         day-to-day operational runbook
docs/API_MANIFEST.md    the versioned manifest contract extract.py consumes
```
