# Runbook

Day-to-day operational reference for the `tuva-ingest` connector. See
`README.md` for architecture and setup; `docs/SOURCE_CONTRACT.md` for
the full paginated extraction/reconciliation/watermark contract
`extract`/`load`/`sync` implement; `docs/API_MANIFEST.md` for the legacy
manifest contract `run`/`load-raw` consume.

## Routine operation

Paginated, endpoint-scoped, one endpoint at a time (the current,
recommended way to operate this connector -- see README.md's
"`extract` / `load` / `sync`" section for the full secret-manager/
pagination/reconciliation/watermark contract):

```bash
uv run tuva-ingest sync --endpoint medical-claims
# resolves the endpoint's last committed watermark automatically, then
# extracts and loads in one command:
# {"event": "sync", "run_id": "medical-claims-20260816T140302-...", "status": "succeeded", "row_count": 1214, "high_water_mark": "2026-08-16T14:03:00Z", ...}
```

Or step by step, for more visibility between extraction and load (e.g.
to inspect the published run before loading it):

```bash
uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
# {"event": "extract", "run_id": "medical-claims-20260816T140302-...", "page_count": 3, "record_count": 1214, ...}
uv run tuva-ingest load --run-id medical-claims-20260816T140302-...
```

An explicit `--since` overrides what is requested for that one
extraction only -- it never lowers the durable watermark; omit it (as in
the `sync` example above) to continue automatically from the last
committed watermark. Repeat for each endpoint (`medical-claims`,
`pharmacy-claims`, `eligibility`) your operational schedule needs -- each
progresses independently; loading one never truncates or touches
another endpoint's raw table, and each endpoint has its own watermark
row in `ingest_ops.source_watermarks`.

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

- **`extract` failed partway through (a page request, envelope
  validation, or a detected pagination cycle)**: the partially staged
  run was already cleaned up (`pagination.PaginatedRunStore.abort_staging`,
  invoked from `extract_paginated_run`'s `finally` block); nothing was
  published. Just rerun `uv run tuva-ingest extract --endpoint <name>`
  -- it mints a fresh `run_id`, so a retry is a wholly new run, not a
  resume.
- **`load`/`sync` failed partway through (checksum/reconciliation
  mismatch, a backward-moving candidate watermark, a connection
  drop)**: the whole transaction (the raw table load, run/table-load
  bookkeeping, and the watermark write) rolled back together; the
  endpoint's previously committed high-water mark is completely
  untouched, and no partial rows are visible in the raw table. Rerun
  `uv run tuva-ingest load --run-id <same value>` -- re-loading an
  already-loaded run is a safe no-op (`ON CONFLICT DO NOTHING`); if the
  underlying cause was a corrupted page file or a source data problem,
  re-run `extract` for a fresh run instead.
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

- Raw snapshots/paginated runs under `RAW_DATA_DIR` are never deleted
  automatically -- prune old ones manually once you no longer need to
  replay them. A published paginated run is likewise never
  auto-deleted; only its temporary `.staging/` directory is cleaned up
  on failure.
- **Paginated (`extract`/`load`/`sync`)**: loading is additive, never a
  replacement -- each run's rows are inserted alongside every prior
  run's rows for that endpoint (`ON CONFLICT DO NOTHING` keyed on
  `(_snapshot_id, _source_row_number)`), so a raw table accumulates
  every successfully loaded run over time. Reloading an already-loaded
  `run_id` is always safe (no duplication).
- **Legacy (`load-raw`/`run`)**: reloading an already-loaded
  `snapshot_id` is always safe (no duplication); reloading a
  *different* `snapshot_id` replaces the raw tables' contents entirely
  (`TRUNCATE` + reload -- each raw table only ever holds the most
  recently loaded legacy snapshot). Mixing both contracts against the
  same raw tables is expected and safe -- they use disjoint metadata
  columns and never truncate rows the other contract loaded (the
  legacy path's `TRUNCATE` does clear the whole table, including any
  paginated rows, so avoid running `load-raw`/`run` against a raw
  schema you are also loading paginated data into, unless a full reset
  is actually what you want).
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

## Local PostgreSQL + MinIO (object-storage-backed workflow)

MinIO is a disposable, local-only S3-compatible object store -- entirely
optional; every command above works unchanged with the default
`OBJECT_STORAGE_PROVIDER=local` (a real-file-I/O local backend, see
`src/tuva_ingest/object_storage/local.py`), which never touches MinIO.
Start it only when you want to exercise `OBJECT_STORAGE_PROVIDER=s3`
locally, e.g. before deploying against real AWS S3:

```bash
docker compose up -d minio                # start MinIO, wait for its healthcheck
docker compose run --rm minio-mc          # idempotent: creates the local dev bucket (tuva-raw-local)
```

Then, in `.env` (or the environment `tuva-ingest` runs in):

```bash
export OBJECT_STORAGE_PROVIDER="s3"
export OBJECT_STORAGE_BUCKET="tuva-raw-local"
export OBJECT_STORAGE_ENDPOINT_URL="http://localhost:9000"
export OBJECT_STORAGE_REGION="us-east-1"
# Ambient credentials only (see src/tuva_ingest/object_storage/s3.py) --
# for this LOCAL MinIO instance only, boto3's ambient chain is satisfied
# by ordinary AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY env vars matching
# compose.yml's MINIO_ROOT_USER/MINIO_ROOT_PASSWORD. Never do this
# against real AWS -- use an IAM role/AWS_PROFILE instead (see "Ambient
# IAM authentication" below).
export AWS_ACCESS_KEY_ID="tuva-local-minio"
export AWS_SECRET_ACCESS_KEY="local-only-example-minio-secret-change-me"
```

```bash
tuva-ingest extract --endpoint eligibility --storage object-storage
tuva-ingest load --run-id <run_id> --storage object-storage
# or in one step:
tuva-ingest sync --endpoint eligibility --storage object-storage
```

### Bucket creation and required settings (production)

- Create the bucket out-of-band (Terraform/CloudFormation/console/CLI --
  this repository does not provision cloud infrastructure).
- Set `OBJECT_STORAGE_PROVIDER=s3`, `OBJECT_STORAGE_BUCKET=<name>`,
  `OBJECT_STORAGE_REGION=<region>`; leave `OBJECT_STORAGE_ENDPOINT_URL`
  unset for real AWS S3 (set it only for a non-AWS S3-compatible
  service).
- **Never** set a static `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` in
  production configuration -- see "Ambient IAM authentication" below.

### Ambient IAM authentication and least-privilege bucket permissions

`object_storage.s3.S3Backend` authenticates only via boto3's default
ambient credential chain (an IAM role attached to the compute the
connector runs on, an assumed role, or `AWS_PROFILE` in local
development) -- this connector never accepts, stores, or logs a static
access-key/secret-key pair for object storage (or, per `secrets.py`, for
the AWS Secrets Manager API credential provider either). Grant the
ingestion role's IAM policy only what it needs on the configured bucket
(least privilege), scoped to the configured `OBJECT_STORAGE_PREFIX`:
`s3:PutObject`, `s3:GetObject`, `s3:ListBucket` (prefix-scoped),
`s3:HeadObject`. Do not grant `s3:DeleteObject` -- this connector never
deletes a published object, and a production bucket policy should not
allow it either (see "PHI implications" below for object versioning as
the correct way to handle any future need to remove an object).

### PHI implications, encryption, retention, versioning, lifecycle, access logging

Every page object is potentially PHI-bearing (it is the source's own
record, byte-for-byte). At minimum for a production bucket:

- **Encryption at rest:** enable default bucket encryption
  (SSE-S3 or SSE-KMS) -- this repository does not configure this for
  you; it is an infrastructure/bucket-policy concern.
- **Encryption in transit:** enforce HTTPS-only bucket policy
  (`aws:SecureTransport`); `S3Backend` itself always uses `boto3`'s
  default HTTPS client.
- **Retention:** define a retention/lifecycle policy appropriate to your
  regulatory obligations -- this repository does not define one; the
  object-key layout's `load_date=`/`run_id=` partitioning makes
  date-scoped lifecycle rules (e.g. transition-to-Glacier or expire
  after N days) straightforward to write against the actual bucket.
- **Object versioning:** enabling bucket versioning is a defense-in-depth
  recommendation (protects against an out-of-band accidental delete or
  overwrite bypassing this connector's own immutability checks) --
  this connector's own immutability guarantee (Section 15 of
  `docs/SOURCE_CONTRACT.md`) does not depend on it, but does not conflict
  with it either.
- **Access logging:** enable S3 server access logging (or CloudTrail data
  events) on the bucket for audit/investigation purposes.
- **Public access:** the bucket must NEVER be public -- block all public
  access at the bucket/account level. This connector's own IAM policy
  recommendation above never requires public access.

### Known limitations (object storage)

- `object_storage.s3.S3Backend`'s conditional-write race-window
  limitation on S3-compatible services that do not support
  `IfNoneMatch` -- see that module's own docstring.
- `macros/generate_schema_name.sql`'s Tuva-package core/marts routing
  heuristic could not be verified against the real pinned package via
  `dbt deps`/`dbt parse` in this repository's own sandboxed development
  environment (no network access to fetch the package) -- verify with
  `dbt list --output json --output-keys unique_id,schema` once you have
  network access, and adjust the macro's `_tuva_core_schema_names` list
  if any Tuva-configured schema name differs from what is assumed.
- `boto3` (needed for BOTH the `aws` secret provider and the `s3` object
  storage backend) is declared in `pyproject.toml`'s `aws` extra but was
  ALREADY absent from `uv.lock` before this change (a pre-existing gap,
  confirmed against `origin/dev`) -- run `uv lock` once you have network
  access to PyPI to resolve and pin it before relying on
  `uv sync --locked --extra aws` for either feature.

## Upgrade notes

**RAW_SCHEMA/OPS_SCHEMA default values changed** as part of adding
object storage (from `raw`/`ingest_ops` to `raw_incoming`/`ops` -- see
`docs/SOURCE_CONTRACT.md` "Six-schema lineage"). An existing deployment
that never explicitly set `RAW_SCHEMA`/`OPS_SCHEMA` (relying on the old
defaults) MUST set them explicitly to its CURRENT values
(`RAW_SCHEMA=raw`, `OPS_SCHEMA=ingest_ops`) before upgrading, or the
connector will start creating/reading a NEW, empty `raw_incoming`/`ops`
schema pair instead of its existing data. A deployment that already sets
either variable explicitly is unaffected -- only the default changed,
never override behavior.

`migrations/006_object_storage_raw_contract.sql` is purely additive
(new tables, new nullable columns, new grants) and safe to apply to any
existing database regardless of whether you adopt the object-storage
workflow -- the legacy CSV and local-filesystem-paginated workflows are
completely unaffected by it.

## Running the test suite

```bash
make test-unit          # database-free (includes object_storage/endpoint_contract/schema_observation tests)
make test-integration   # requires PG_DSN pointed at a DISPOSABLE database -- never production
make quality             # full database-free quality gate (lock check, lint, types, unit tests, sql lint)
make ci-full              # quality + dbt parse/deps + the full integration suite
```

Object-storage-specific suites (also collected by the targets above,
listed here for direct/opt-in invocation):

```bash
# Deterministic, database-free, always run by `make test-unit`:
python3 -m pytest tests/unit/test_object_storage_keys.py tests/unit/test_object_storage_backends.py \
  tests/unit/test_object_storage_publish_verify.py tests/unit/test_endpoint_contract.py \
  tests/unit/test_schema_observation.py tests/unit/test_state_object_storage.py \
  tests/unit/test_object_raw_loader.py -v

# Requires PG_DSN (disposable database):
PG_DSN=postgresql://user:pass@host:port/db \
  python3 -m pytest tests/integration/test_object_storage_pipeline_integration.py -v

# Opt-in, requires a running MinIO (docker compose up -d minio && docker compose run --rm minio-mc):
OBJECT_STORAGE_TEST_ENDPOINT_URL=http://localhost:9000 \
OBJECT_STORAGE_TEST_BUCKET=tuva-raw-local \
OBJECT_STORAGE_TEST_ACCESS_KEY_ID=tuva-local-minio \
OBJECT_STORAGE_TEST_SECRET_ACCESS_KEY=local-only-example-minio-secret-change-me \
  python3 -m pytest tests/integration/test_object_storage_minio_integration.py -v
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
- Rotate the API credential by updating `TUVA_API_TOKEN` in `.env`/your
  secret store (`TUVA_API_SECRET_PROVIDER=env`), or by rotating the
  secret's value in AWS Secrets Manager (`TUVA_API_SECRET_PROVIDER=aws`)
  -- no code change is required either way. The credential is retrieved
  fresh at the start of every `extract`/`sync` run, never cached to
  disk between runs.
- This repository's own test fixtures (`tests/fixtures/*.csv`) are
  synthetic and contain no PHI; do not commit real extracted snapshots
  or database dumps to this repository.
- Object storage credentials are never static: `object_storage.s3.S3Backend`
  authenticates only via boto3's ambient credential chain (see "Ambient
  IAM authentication" above) and never logs a credential value -- the
  same posture `secrets.py` already applies to the API token.
- `ops.rejected_record` is PHI-bearing; `transform_role` (dbt) has no
  grant on it at all, and PUBLIC is explicitly revoked
  (`migrations/006_object_storage_raw_contract.sql`) -- see
  `docs/SOURCE_CONTRACT.md` "Rejected-record investigation" for the
  supported way to inspect a rejected record.

## What this repository does not own

This repository does not define, migrate, or reproduce any Tuva-managed
core, terminology, or output table. If you need to inspect Tuva's own
schema, look at `dbt_packages/the_tuva_project/` after running `dbt
deps` -- never add equivalent DDL here.
