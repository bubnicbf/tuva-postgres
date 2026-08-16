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
   versioned JSON manifest (see `docs/API_MANIFEST.md`; for the full
   operational source contract -- auth, pagination, rate limits,
   incremental/mutability semantics, PHI, reconciliation -- see
   `docs/SOURCE_CONTRACT.md`) describing one snapshot's per-table CSV
   artifacts, downloads each one
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

`tests/unit/` fakes or mocks every I/O boundary (an in-process
`http.server` for the API client, fake psycopg connections for
SQL-composition tests) so it never touches a real database or network.
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
src/tuva_ingest/     the connector: config, api_client, extract, raw_loader, state, migrations, cli
migrations/           001-003: raw + operational schemas, run/table-load control, role grants
dbt_project.yml, packages.yml, profiles.example.yml, macros/, models/   the dbt project
tests/unit/            database-free tests (including test_input_layer_contract.py's
                        file-level Input Layer contract checks)
tests/integration/     tests requiring a disposable PostgreSQL database
tests/fixtures/         small synthetic CSV fixtures used by tests/integration
docs/RUNBOOK.md         day-to-day operational runbook
docs/API_MANIFEST.md    the versioned manifest contract extract.py consumes
```
