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

1. **Extract** (`tuva-ingest extract --endpoint <name> [--since <date>]`)
   -- fetches and validates a versioned JSON manifest scoped to exactly
   one endpoint (see `docs/API_MANIFEST.md`; for the full operational
   source contract -- auth, pagination, rate limits, incremental/
   mutability semantics, PHI, reconciliation -- see
   `docs/SOURCE_CONTRACT.md`), downloads that one artifact
   (checksum-verified, bounded-retried via `httpx` + `tenacity`,
   bearer-authenticated), and publishes the snapshot atomically under
   `RAW_DATA_DIR`. A partial download can never appear complete to a
   later step. Prints a JSON result including a stable `run_id` (see
   "Run IDs" below).
2. **Load** (`tuva-ingest load --run-id <value>`) -- resolves the exact
   extraction `extract` published, verifies its success marker and
   checksums, and transactionally loads only that one endpoint's raw
   table (`RAW_SCHEMA`, default `raw`) -- never truncating or touching
   any other raw table. Every row is stored as a single `raw_row jsonb`
   column plus fixed metadata columns (`_snapshot_id`,
   `_source_row_number`, `_loaded_at`) -- no type coercion, renaming, or
   business logic happens here, so the raw layer is a faithful,
   replayable copy of exactly what the source sent. Safe to repeat for
   the same `run_id` (idempotent).
3. **Sync** (`tuva-ingest sync --endpoint <name> [--since <date>]`) --
   `extract` then `load`, for one endpoint, in a single command. Stops
   immediately (nonzero exit, `load` never attempted) if `extract`
   fails.
4. **dbt** (`tuva-ingest dbt -- <args>`) -- `models/staging/*.sql`
   types and normalizes the raw JSONB into typed columns;
   `models/final/{eligibility,medical_claim,pharmacy_claim}.sql`
   expose the Tuva Input Layer contract those staging models feed. dbt
   never writes back into the raw schema.
5. **Tuva package** -- pinned to exactly `0.18.0`
   (`packages.yml`), `ref()`s this project's Input Layer models by
   name and builds its own core/terminology/mart models on top of
   them, in its own schema(s). This repository never vendors or
   duplicates any of that package's model files.

Source data is never loaded directly into any Tuva-managed schema.

**Legacy full-pipeline commands** (`tuva-ingest run`, and
`tuva-ingest load-raw`, which `run` calls internally) still fetch and
load all three raw tables from a single manifest in one call each --
kept, documented, and tested for backward compatibility (see "Backward
compatibility" below). The `extract`/`load`/`sync` commands above are the
current, endpoint-scoped way to operate this connector.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12, pinned via
  `.python-version`)
- Docker + Docker Compose v2, for a local disposable PostgreSQL (or
  point `PG_DSN` at any PostgreSQL 16+ instance you already have)
- Network access to dbt Hub (for `dbt deps`, which fetches the pinned
  Tuva package) when you actually run dbt

## Dependency choices

| Concern | Library |
| --- | --- |
| HTTP client (manifest fetch, streaming artifact downloads) | [`httpx`](https://www.python-httpx.org/) -- a reusable, explicitly-timed-out, synchronous `httpx.Client` |
| Bounded retries | [`tenacity`](https://tenacity.readthedocs.io/) -- explicit `stop_after_attempt`/custom bounded-backoff `wait`, never an unbounded loop |
| Configuration | [`pydantic-settings`](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) -- environment-driven, typed, validated, with `SecretStr` for credentials |
| PostgreSQL driver | [`psycopg`](https://www.psycopg.org/psycopg3/) 3, parameterized queries + `psycopg.sql`/validated identifier helpers for dynamic SQL |
| Structured logging | stdlib `logging` + `json` (see `src/tuva_ingest/logging_utils.py`) -- no extra dependency |
| dbt orchestration | `dbt-core`/`dbt-postgres`, shelled out to via `tuva-ingest dbt -- <args>` |
| Testing | `pytest`, `httpx.MockTransport` (no live server, no real network) |

`requests` (and its `types-requests` type-stub dependency) have been
fully removed -- `httpx` is now the only HTTP client dependency.

## Quick start

```bash
git clone <this repo> && cd tuva-postgres
make init                      # uv sync --locked; installs pre-commit; copies .env template
# edit .env: at minimum PG_DSN, and TUVA_API_MANIFEST_URL/TUVA_API_TOKEN
# if you plan to run `extract`/`sync` against a real manifest endpoint

make local-db-ready            # starts a local disposable Postgres (docker compose) and migrates it
make migrate-status            # read-only: confirm 001-004 are applied

# Endpoint-scoped extract -> load (the current, recommended workflow):
uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
# -> prints {"event": "extract", "run_id": "...", "endpoint": "medical-claims", ...}
uv run tuva-ingest load --run-id 019...            # the run_id extract printed
# ...or do both in one command:
uv run tuva-ingest sync --endpoint medical-claims --since 2025-01-01

make dbt-deps                  # fetch the pinned Tuva 0.18.0 package
make dbt-build                 # staging -> Input Layer -> Tuva's own models, plus dbt tests

make health                    # DB connectivity + migration state + last-successful-run freshness
```

Or run the whole legacy full-manifest pipeline in one command once `.env`
is populated (see "Backward compatibility" below):

```bash
make run   # migrate -> extract (all 3 tables) -> load-raw -> dbt deps -> dbt build
```

## Configuration

Every setting is an environment variable, loaded and validated by
`src/tuva_ingest/config.py`'s `IngestConfig` -- a `pydantic-settings`
`BaseSettings` subclass, constructed via `IngestConfig.load(required=...)`
(never the bare pydantic constructor) -- see `scripts/setup_env.example`
for the full list with safe local defaults. `make init` copies it to
`.env` (git-ignored) for you; `IngestConfig` reads real process
environment variables first, then that `.env` file if present, then each
field's own default (standard `pydantic-settings` precedence).

| Variable | Purpose | Default |
| --- | --- | --- |
| `PG_DSN` | PostgreSQL connection string (`SecretStr`) | *(required for DB commands)* |
| `RAW_SCHEMA` | Raw landing schema (never Tuva-managed) | `raw` |
| `OPS_SCHEMA` | Operational/control schema (run/table-load history) | `ingest_ops` |
| `INPUT_LAYER_SCHEMA` | Schema dbt materializes `models/final/*.sql` into | `input_layer` |
| `INGEST_ROLE` / `TRANSFORM_ROLE` | Least-privilege role names (see `migrations/003_roles_and_grants.sql`) | `tuva_ingest_role` / `tuva_transform_role` |
| `TUVA_API_MANIFEST_URL` / `TUVA_API_TOKEN` | Manifest endpoint + bearer token (`SecretStr`; see `docs/API_MANIFEST.md`) | *(required for `extract`/`sync`/`run`)* |
| `TUVA_API_TIMEOUT_SECONDS` | Fallback httpx timeout (all phases) | `30` |
| `TUVA_API_CONNECT_TIMEOUT_SECONDS` / `TUVA_API_READ_TIMEOUT_SECONDS` / `TUVA_API_WRITE_TIMEOUT_SECONDS` / `TUVA_API_POOL_TIMEOUT_SECONDS` | Per-phase httpx timeout overrides (optional; each falls back to `TUVA_API_TIMEOUT_SECONDS`) | unset |
| `TUVA_API_MAX_RETRIES` | Max additional attempts after the first (bounded; never unbounded) | `5` |
| `TUVA_API_MAX_RETRY_DELAY_SECONDS` | Hard ceiling on any single retry sleep, including a `Retry-After` value | `30` |
| `TUVA_API_ALLOW_INSECURE_HTTP` | Allow `http://` manifest/artifact URLs (local mock servers only) | `0` |
| `RAW_DATA_DIR` | Local extraction/snapshot directory | `data/raw` |
| `SOURCE_NAME` | Top-level directory name under `RAW_DATA_DIR` | `tuva` |
| `DBT_TARGET` / `DBT_PROFILES_DIR` / `DBT_PROJECT_DIR` | Passed through to every `dbt` invocation | `dev` / `.` / `.` |
| `PIPELINE_ENVIRONMENT` / `PIPELINE_MAX_SUCCESS_AGE_HOURS` | Healthcheck freshness window | `local` / `30` |
| `LOG_LEVEL` | Structured JSON log level | `INFO` |

`IngestConfig.load(required=...)` fails fast with every problem listed at
once (never just the first) -- combining pydantic's own type/range/
identifier validation (every dynamic schema/role name is validated
against a single shared identifier policy,
`src/tuva_ingest/identifiers.py`, via a pydantic field validator, before
any SQL is composed) with command-scoped required-field checks (e.g. a
command that only needs PostgreSQL, like `migrate`/`healthcheck`, never
requires `TUVA_API_TOKEN`). `PG_DSN` and `TUVA_API_TOKEN` are
`pydantic.SecretStr` -- never a plain `str` -- so `repr()`, `str()`,
pydantic's own validation-error rendering, and `IngestConfig.safe_dict()`
can never accidentally include the real value; call `.pg_dsn_value` /
`.api_token_value` explicitly at the one point an actual connection/
request needs the real value.

## `extract` / `load` / `sync`: endpoints, run IDs, retries, JSON output

### Endpoint names

| `--endpoint` | Raw table | Tuva Input Layer model |
| --- | --- | --- |
| `medical-claims` | `medical_claim` | `models/final/medical_claim.sql` |
| `pharmacy-claims` | `pharmacy_claim` | `models/final/pharmacy_claim.sql` |
| `eligibility` | `eligibility` | `models/final/eligibility.sql` |

(`src/tuva_ingest/endpoints.py` is the single source of truth for this
mapping.) `--endpoint` is required for `extract`/`sync` and restricted to
these three values -- an unknown endpoint, or a malformed `--since`
(anything other than `YYYY-MM-DD`), is rejected locally before any HTTP
request is made.

### Run IDs

A successful `extract` reuses the manifest's own immutable `snapshot_id`
as the run id -- printed as `run_id` in its JSON result. Because the
on-disk snapshot layout (`RAW_DATA_DIR/<source>/<snapshot_id>/`) is
already keyed by that same value, `tuva-ingest load --run-id <value>`
can always resolve it directly from disk after the `extract` process has
exited, with no separate database lookup or run-id-to-snapshot mapping
to keep in sync. `load` verifies the run's `_SUCCESS` marker and
per-table checksums before loading, and is safe/deterministic to call
again for the same `run_id` (see "Failure recovery and idempotency"
below).

### Retry and timeout behavior

`src/tuva_ingest/api_client.py`'s `ApiClient` (a reusable `httpx.Client`)
retries only genuinely transient failures, via `tenacity`:

* httpx connection/timeout errors (`httpx.NetworkError`, `httpx.TimeoutException`)
* HTTP `429`
* HTTP `500`, `502`, `503`, `504`

Ordinary client errors (`400`, `401`, `403`, `404`), validation failures,
and checksum failures are **never** retried. Retries are bounded by
`TUVA_API_MAX_RETRIES` (never unbounded) with exponential backoff and
jitter, honoring a valid `Retry-After` header -- but every sleep,
whichever source produced it, is capped at
`TUVA_API_MAX_RETRY_DELAY_SECONDS`. Connect/read/write/pool timeouts are
each independently configurable (`TUVA_API_CONNECT_TIMEOUT_SECONDS` etc.,
falling back to `TUVA_API_TIMEOUT_SECONDS`). Redirects are never followed
(`follow_redirects=False`) -- a manifest/artifact URL redirecting to an
unexpected host must never silently receive this client's bearer token.

### JSON output and logging

`extract`/`load`/`sync` each print exactly one JSON object to stdout on
success:

```bash
$ uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
{"endpoint": "medical-claims", "event": "extract", "path": "data/raw/tuva/019...", "run_id": "019...", "since": "2025-01-01", "status": "succeeded", "table": "medical_claim"}

$ uv run tuva-ingest load --run-id 019...
{"endpoint": "medical-claims", "event": "load", "path": "data/raw/tuva/019...", "row_count": 4213, "run_id": "019...", "since": "2025-01-01", "status": "succeeded", "table": "medical_claim"}
```

Human-readable diagnostics (progress, retries) go to structured JSON log
lines (also on stdout, one line per event -- see below) or to stderr for
fatal errors; a caller scripting against `tuva-ingest` should parse only
the final stdout line as the command's result. Every failure -- a
validation error, an HTTP failure, a checksum mismatch, a database
error -- is a nonzero exit code with a single sanitized stderr line
(`ERROR [<category>]: <message>`); `sync` never prints a success result
after a partial failure (`extract` failing stops it before `load` is ever
attempted).

Every log line is one JSON object (`src/tuva_ingest/logging_utils.py`),
UTC ISO-8601 timestamped, with `event`/`level`/`app_version` always
present and `run_id`/`endpoint`/`stage`/`duration_ms`/`table`/
`error_category`/`error_message` where applicable:

```json
{"app_version": "0.1.0", "endpoint": "medical-claims", "event": "artifact_download_completed", "duration_ms": 842.1, "level": "INFO", "run_id": "019...", "table": "medical_claim", "timestamp": "2026-08-16T14:03:02.114000Z"}
```

`TUVA_API_TOKEN`/`PG_DSN`/`Authorization` header values are never present
in any log line, JSON result, or exception message (see "Security"
below).

## Backward compatibility

`extract`/`load`/`sync` are the current, endpoint-scoped commands. Every
previously existing command still works, unchanged, and is tested for
compatibility (`tests/unit/test_cli.py`'s `TestBuildParser`,
`tests/integration/test_pipeline_integration.py`):

| Command | Status | Behavior |
| --- | --- | --- |
| `tuva-ingest migrate` | unchanged | apply/inspect operational migrations |
| `tuva-ingest dbt -- <args>` | unchanged | shell out to `dbt` with this connector's vars/target |
| `tuva-ingest healthcheck` | unchanged | DB connectivity + migration state + run freshness |
| `tuva-ingest run` | legacy, retained | full pipeline: fetch a manifest for all three tables in one request -> load all three -> `dbt deps` -> `dbt build` |
| `tuva-ingest load-raw [--snapshot-id ...]` | legacy, retained | load a full (all-three-table) snapshot into the raw schema; defaults to the `current` published snapshot |
| `tuva-ingest extract` (no `--endpoint`) | **removed** -- `--endpoint` is now required | use `tuva-ingest run` for the equivalent full-manifest extraction, or `extract --endpoint <name>` for one endpoint |

`load --run-id` and `load-raw` are deliberately separate commands, not
one command with inferred behavior: `load --run-id` only ever resolves
an endpoint-scoped `extract` run (one table); `load-raw` only ever
resolves a full, all-three-table manifest snapshot (from `extract`
without `--endpoint`, which no longer exists as a CLI form, or from a
`run` in progress). Pointing `load --run-id` at a legacy snapshot -- or
`load-raw` at an endpoint-scoped one -- fails loudly
(`RunNotFoundError`) rather than silently loading the wrong shape of
data.

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
`003_roles_and_grants.sql`, and `004_endpoint_scoped_ingestion.sql` are
the only DDL this repository owns: the raw and operational-control
schemas, run/table-load bookkeeping tables, least-privilege role grants,
and (004) the `endpoint`/`requested_since` columns on `ingestion_runs`
plus the unique index on `table_loads (run_id, table_name)` that makes
`tuva-ingest load --run-id ...` safe to repeat. They are checksum-tracked
(`src/tuva_ingest/migrations.py`), applied at most once each, and
rerunning `tuva-ingest migrate` against an already-migrated database is
always a true no-op. Dynamic identifiers (schema/role names) use
psql-style `:"name"` substitution, validated against the same shared
identifier policy every other dynamic-SQL call site uses -- see
`migrations.py`'s module docstring for why static SQL alone can't
express this. Migrations are immutable once applied -- 004 only adds
nullable columns and a new index; it never rewrites 001-003, and any
future change is a new, forward-only, numbered migration file.

## dbt project

- `dbt_project.yml` -- claims-only Tuva domain configuration
  (`claims_enabled: true`; `clinical_enabled`/`provider_attribution_enabled`/
  `semantic_layer_enabled: false`) and the
  `require_ref_searches_node_package_before_root` flag Tuva 0.18.0
  requires. Both `models.tuva_ingest_connector.staging` and `.final`
  carry `+tags: ["input_layer"]` -- not just `final` -- so `dbt build
  --select tag:input_layer` can build this connector's entire
  transformation pipeline (raw -> staging -> Input Layer) from an empty
  database in one pass, matching the pattern used by Tuva's own
  [connector_template](https://github.com/tuva-health/connector_template).
- `packages.yml` -- `tuva-health/the_tuva_project` pinned to exactly
  `0.18.0` (never a range, `main`, `latest`, or an unpinned git
  revision), plus `dbt_utils` (used by `models/final/schema.yml`'s
  composite-primary-key uniqueness tests).
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
  `TRY_CAST`) using the Input Layer's own column names directly
  (`person_id`, `drg_code`/`drg_code_type`, `hcpcs_code`, etc. -- not an
  internal naming convention that models/final/ then has to translate).
  No Tuva-specific business logic here.
- `models/final/*.sql` -- the full Tuva 0.18.0 Input Layer contract for
  each of the three claims models (35/148/25 explicit, ordered columns
  for `eligibility`/`medical_claim`/`pharmacy_claim` respectively -- see
  each model's own header comment for the two independent official
  sources used to confirm that column list, and `models/final/
  schema.yml` for the composite-primary-key test on each). Fields this
  connector's source data cannot confidently supply are explicitly
  typed `NULL` (e.g. `cast(null as text)`) -- documented in each
  model's own header comment -- never an invented mapping. The 100
  numbered `diagnosis_code_N`/`diagnosis_poa_N`/`procedure_code_N`/
  `procedure_date_N` columns on `medical_claim` are generated by a
  Jinja `{%- for i in range(1, 26) %}` loop purely to avoid 100 lines
  of repetition; the compiled SQL is the same fully explicit, named
  column list as if they were typed out by hand (never a
  `select *`).

## Testing

```bash
make test-unit          # database-free: fakes/mocks only, safe to run anywhere
make test-integration   # requires PG_DSN pointed at a DISPOSABLE PostgreSQL database
make quality            # dependency lock check, ruff, mypy, unit tests, sqlfluff -- database-free
make ci-full            # quality + dbt parse/deps + the full integration suite
make pipeline           # the complete, stop-on-first-failure validation order below
```

`tests/unit/` fakes or mocks every I/O boundary (`httpx.MockTransport`
for the API client -- no real socket, live server, or external API --
fake psycopg connections for SQL-composition tests) so it never touches
a real database or network. `tests/unit/test_config.py` covers
`pydantic-settings` type/range/identifier validation and secret
redaction; `tests/unit/test_endpoints.py` covers the `--endpoint` <->
raw table mapping; `tests/unit/test_logging_utils.py` proves every
emitted structured log line is valid JSON and that secrets never appear
in it.
`tests/unit/test_input_layer_contract.py` is also database-free and
network-free, but specifically enforces the Input Layer contract at the
file level by parsing `packages.yml`/`dbt_project.yml`/the model SQL/
`models/final/schema.yml`/the Makefile/CI workflow directly: the Tuva
pin, domain-enablement vars, the three required final models (present,
`input_layer`-tagged, no `select *`, every contract column named
explicitly, every synthetic NULL explicitly typed), the composite
primary-key tests, that nothing writes into a Tuva-managed schema name,
that `profiles.example.yml` never hard-codes a credential, and that
structural DQ is always invoked (in the Makefile, the CLI, and CI)
before any logical/analytical DQ step.

`tests/integration/test_pipeline_integration.py` requires a real,
disposable PostgreSQL database (never point it at production) and
proves: migrations are truly idempotent on a second run; reloading the
same snapshot never duplicates rows; a corrupted checksum rolls back
the *entire* snapshot's transaction, not just one table; ingestion
never creates or touches any Tuva-managed schema name; and (when `dbt`
is on `PATH`, which `uv sync --locked` guarantees) a real
`dbt build --select tag:input_layer` against the fixtures in
`tests/fixtures/` produces the three Input Layer tables with the
expected row counts, that `dbt build --select tag:dq_structural`
subsequently passes, that every required Tuva 0.18.0 column exists on
each relation (via `information_schema.columns`) with a compatible
PostgreSQL type, and that two consecutive full-refresh builds produce
identical row counts (determinism).

## Validation order

Structural DQ must pass before logical or analytical DQ, and logical/
analytical DQ (when configured) must pass before any downstream Tuva
mart or reporting workflow runs. A structural failure means the Input
Layer's *shape* (required models/columns/types/keys) is wrong, which
makes any value-level check downstream meaningless -- so this repository
never runs a later stage after an earlier one fails, in any of its three
orchestration surfaces:

1. `dbt debug` -- connection/profile sanity (`make dbt-debug`)
2. `dbt deps` -- fetch the pinned Tuva package (`make dbt-deps`)
3. `dbt parse` -- Jinja/YAML/ref() validation, no database needed (`make dbt-parse`)
4. `dbt build --select tag:input_layer` -- this connector's own models (`make dbt-input-layer`)
5. `dbt build --select tag:dq_structural` -- the pinned Tuva package's structural DQ (`make dbt-dq-structural`)
6. Logical DQ, if configured (`make dbt-dq-logical`)
7. Analytical DQ, if configured (`make dbt-dq-analytical`)
8. Downstream Tuva models / reporting workflows

`make pipeline` runs stages 1-5 as a single stop-on-first-failure
command; `tuva-ingest run`/`make run` runs the same stage 4 -> 5 order
after `extract`/`load-raw` (see `src/tuva_ingest/cli.py`'s `_cmd_run`);
`.github/workflows/ci.yml`'s `integration` job runs the same order
before its full `pytest tests/integration` suite. See
`tests/unit/test_input_layer_contract.py`'s `TestValidationOrdering`
for the tests that enforce this ordering in all three places without
requiring dbt or a database.

**Known limitation on `tag:dq_structural` specifically:** this
repository's own sandboxed development environment (used to build this
PR) has no outbound network access to dbt Hub/PyPI and no local
PostgreSQL/dbt installation, so `dbt deps`/`dbt build --select
tag:dq_structural` could not actually be executed here to confirm that
selector's exact node set against the live pinned package (see "Known
limitations" below for the full record). Before relying on this
selector in production, run `dbt deps` followed by `dbt ls --select
tag:dq_structural` in a network-enabled environment and confirm it
selects a non-empty, expected set of structural checks --
`TestDbtLineageAgainstRealDatabase` in `tests/integration/
test_pipeline_integration.py` fails loudly (never silently) if it does
not.

## Actual generated PostgreSQL schema names

dbt's default custom-schema behavior concatenates the target schema
with a model's configured `+schema` (e.g. `dbt_dev_staging`), which is
**not** what this project wants. `macros/generate_schema_name.sql`
overrides that default so a model's configured `+schema` is used
*exactly*, never prefixed:

| Models | Configured `+schema` | Actual PostgreSQL schema |
| --- | --- | --- |
| `models/staging/*.sql` | `staging` (`dbt_project.yml`) | `staging` |
| `models/final/*.sql` | `{{ var('input_layer_schema') }}` (default `input_layer`) | `input_layer` (or whatever `INPUT_LAYER_SCHEMA`/`input_layer_schema` is set to) |
| Any dbt model with no `+schema` override (none exist in this project's own models today) | *(none)* | the profile's own `schema:` (e.g. `dbt_dev`/`dbt_ci`) |
| The pinned Tuva package's own models | whatever `the_tuva_project` itself configures (`core`, `terminology`, data mart schemas, etc.) | unchanged by this project -- `generate_schema_name.sql` only affects schema names *this project's own* models resolve, never the pinned package's |

Confirm this after a real `dbt build` with:

```sql
select table_schema, table_name from information_schema.tables
where table_name in ('eligibility', 'medical_claim', 'pharmacy_claim');
```

## How to add another Tuva domain safely

1. Read [thetuvaproject.com/input-layer](https://thetuvaproject.com/input-layer)
   and the domain's own mapping guide (e.g. clinical) for the **complete**
   list of required models and columns for that domain in the pinned
   version (`packages.yml`).
2. Create every required model under `models/final/` -- even ones this
   connector's source cannot populate at all. A model with zero source
   rows must still exist as a structurally valid, empty relation: e.g.
   `select ... cast(null as text) as some_column ... where false` (or
   `limit 0`), with every contract column present and correctly typed.
3. For a populated model, every column the source cannot supply must be
   an explicitly typed `NULL` (`cast(null as <type>)`), never a bare
   `null` and never an invented value.
4. Add `+tags: ["input_layer"]` coverage (already inherited from
   `dbt_project.yml`'s `models.tuva_ingest_connector.final` block if the
   new models live under `models/final/`) and a composite-key
   `dbt_utils.unique_combination_of_columns` test (or single-column
   `unique`, if the domain's primary key is one column) in a
   `schema.yml` alongside the new models.
5. Only then flip that domain's `*_enabled` var to `true` in
   `dbt_project.yml`. Never flip it on before every required model and
   column for that domain exists -- an enabled domain with a partial
   interface is exactly what this repository's structural DQ gate
   (`dbt build --select tag:dq_structural`) exists to catch, and
   `tests/unit/test_input_layer_contract.py`'s
   `test_unimplemented_domains_stay_disabled` will fail the build until
   you update it to match your new, deliberate decision.
6. Run the full validation order (`make pipeline`) and extend
   `tests/integration/test_pipeline_integration.py`'s column-contract
   assertions to cover the new domain's models.

## Failure recovery and idempotency

- **Extraction**: a snapshot is only "published" once its artifact(s)
  are downloaded and verified and a `_SUCCESS` marker is written.
  Re-extracting the exact same manifest (same `--endpoint`/`--since`,
  same `snapshot_id`) is a safe no-op; re-extracting a `snapshot_id`
  with *different* content is a loud failure, never a silent overwrite.
- **Endpoint-scoped loading** (`tuva-ingest load --run-id ...`): the
  one named table is `TRUNCATE`d and reloaded inside one transaction
  spanning that table plus run/table-load bookkeeping -- no other raw
  table is ever truncated, queried, or written. Repeating
  `load --run-id <same value>` is safe and deterministic:
  `state.upsert_running_run`/`state.upsert_table_load_pending` reset
  the existing `ingestion_runs`/`table_loads` rows for that `run_id`
  back to `running`/`pending` (`ON CONFLICT ... DO UPDATE`, backed by
  migrations/004's unique index) rather than erroring on a duplicate
  primary key, and the TRUNCATE+COPY itself is naturally idempotent
  (retrying never duplicates rows).
- **Legacy full-manifest loading** (`tuva-ingest load-raw`): all three
  raw tables are `TRUNCATE`d and reloaded from a specific `snapshot_id`
  inside one transaction spanning all three tables plus run
  bookkeeping -- retrying never duplicates rows, and a failure partway
  through never leaves a partially-loaded snapshot visible.
- **Migrations**: checksum-tracked and applied at most once;
  `apply_pending` takes a PostgreSQL advisory lock so concurrent runs
  never race.
- **Run state**: `ingest_ops.ingestion_runs`/`table_loads` (see
  `migrations/002_ingestion_control.sql`, extended by
  `migrations/004_endpoint_scoped_ingestion.sql`) record every run's
  stage, status, endpoint, row counts, and errors. A run can only leave
  `running` exactly once per attempt (see `src/tuva_ingest/state.py`).

## Security

- Never commit `.env` or `profiles.yml` (both git-ignored).
- `PG_DSN` and `TUVA_API_TOKEN` are `pydantic.SecretStr` in
  `IngestConfig` -- never a plain `str` -- so `repr()`/`str()`/pydantic
  validation-error output can never accidentally include the real
  value; both are also never logged, printed, or included in any error
  message via `src/tuva_ingest/logging_utils.py`'s
  `sanitize_error`/`sanitize_text` (defense in depth on top of the
  `SecretStr` type itself).
- HTTPS is required by default for `TUVA_API_MANIFEST_URL`;
  `TUVA_API_ALLOW_INSECURE_HTTP=1` is the one explicit, documented
  escape hatch for local tests against a mock server.
- Redirects are never followed by the httpx client
  (`follow_redirects=False`) -- a manifest/artifact URL cannot silently
  redirect this client's bearer token to an unexpected host.
- Every dynamic schema/table/role identifier is validated against
  `src/tuva_ingest/identifiers.py`'s shared policy before it is ever
  composed into SQL text; data values (including `run_id`, `endpoint`,
  and `since`) are always bound through parameterized queries or
  psycopg's `COPY` protocol, never interpolated.
- This connector never handles PHI in its own test fixtures
  (`tests/fixtures/*.csv` are synthetic).

## Upgrading beyond Tuva 0.18.0

1. Bump the pin in `packages.yml` to the new exact version (never a
   range, `main`, or `latest`) and run `dbt deps`.
2. Regenerate the contract inventory: re-read
   [thetuvaproject.com/connectors/claims-mapping-guide](https://thetuvaproject.com/connectors/claims-mapping-guide)
   and [tuva-health/connector_template](https://github.com/tuva-health/connector_template)'s
   (or whichever reference connector is current) `eligibility`/
   `medical_claim`/`pharmacy_claim` models for the new version, and diff
   that column list against `models/final/*.sql`'s current column set
   (`tests/unit/test_input_layer_contract.py`'s `ELIGIBILITY_CONTRACT_COLUMNS`/
   `MEDICAL_CLAIM_CONTRACT_COLUMNS`/`PHARMACY_CLAIM_CONTRACT_COLUMNS`
   constants are the single source of truth to update first -- the test
   will then fail until `models/final/*.sql` is updated to match).
3. Update `models/final/*.sql` and `models/final/schema.yml` for any
   added/removed/renamed/retyped columns, keeping the same "typed NULL
   for anything this source can't supply" discipline.
4. Re-run the full validation order (`make pipeline`, then
   `make test-integration` against a disposable database) and confirm
   `dbt build --select tag:dq_structural` still passes before merging.
5. Never touch `models/final/*.sql` for a version bump based on this
   repository's own prior SQL alone -- always re-derive the contract
   from the new version's own official source, per the same policy this
   PR followed (see "Known limitations" below).

## Known limitations

- **`tag:dq_structural`'s exact node selection was not empirically
  confirmed against the live pinned package.** This repository's
  sandboxed development environment (used to build this PR) has no
  outbound network access to dbt Hub/PyPI/GitHub and no local
  PostgreSQL or dbt installation, so `dbt debug`/`dbt deps`/`dbt parse`/
  `dbt build --select tag:input_layer`/`dbt build --select
  tag:dq_structural` could not be executed there. The Input Layer
  column contract itself (`models/final/*.sql`) was independently
  confirmed against two current official Tuva sources instead (see each
  model's header comment): the maintained
  [claims mapping guide](https://thetuvaproject.com/connectors/claims-mapping-guide)
  prose, and [tuva-health/connector_template](https://github.com/tuva-health/connector_template)'s
  verified reference implementation (accessed via its GitHub source and
  DeepWiki-rendered documentation). Run `make pipeline` in a
  network-enabled environment with a disposable PostgreSQL database as
  the first real validation of `tag:dq_structural` before relying on it
  in production; `tests/integration/test_pipeline_integration.py`'s
  `TestDbtLineageAgainstRealDatabase` fails loudly (never silently) if
  that selector turns out to match zero nodes.
- **`state`/`fips_state_code`**: this connector's eligibility source
  provides a numeric FIPS state code, not the Input Layer contract's
  2-letter USPS state abbreviation. Rather than invent a FIPS-to-
  abbreviation crosswalk, `state` is left an explicitly typed `NULL`
  and the source value is retained, unused by `models/final/
  eligibility.sql`, as `stg_eligibility.fips_state_code` for lineage
  visibility only.
- Every other field this connector's source data cannot confidently
  supply is an explicitly typed `NULL` (see each `models/final/*.sql`
  header comment for the itemized list per model), never a guess or an
  invented value.

## Repository layout

```
src/tuva_ingest/     the connector: config (pydantic-settings), api_client (httpx+tenacity),
                     endpoints (--endpoint <-> raw table mapping), extract, raw_loader, state,
                     migrations, cli
migrations/           001-004: raw + operational schemas, run/table-load control, role grants,
                     endpoint-scoped ingestion metadata
dbt_project.yml, packages.yml, profiles.example.yml, macros/, models/   the dbt project
tests/unit/            database-free tests (including test_input_layer_contract.py's
                        file-level Input Layer contract checks)
tests/integration/     tests requiring a disposable PostgreSQL database
tests/fixtures/         small synthetic CSV fixtures used by tests/integration
docs/RUNBOOK.md         day-to-day operational runbook
docs/API_MANIFEST.md    the versioned manifest contract extract.py consumes
```
