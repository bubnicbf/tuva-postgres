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
   -- retrieves an API credential once from the configured secret
   manager (`src/tuva_ingest/secrets.py`; see "Secret manager" below),
   then requests one page at a time from `TUVA_API_MANIFEST_URL` (the
   full page-request/envelope/pagination/reconciliation/watermark
   contract lives in `docs/SOURCE_CONTRACT.md`; `docs/API_MANIFEST.md`
   covers only the legacy `run`/`load-raw` manifest shape). Every page
   is validated (envelope shape, metadata, declared vs. actual record
   count, token progression -- a repeated page token is treated as a
   cycle and fails loudly) before being written, byte-for-byte
   unmodified, to an immutable gzip-compressed JSONL file. Pagination
   never prefetches and stops only when the API explicitly signals
   completion (a missing/null `next_page_token`), bounded by
   `TUVA_API_MAX_PAGES` as a final safety net. A run manifest recording
   every page's checksum, record count, and token is published
   atomically, success marker last, under `RAW_DATA_DIR`. Prints a JSON
   result including a fresh `run_id` (see "Run identifiers" below).
2. **Load** (`tuva-ingest load --run-id <value>`) -- independently
   re-verifies the published run's manifest and every page's checksum
   and record count (never trusts extraction's own in-memory state),
   then, inside one database transaction: loads the run's pages into
   that one endpoint's raw table (`RAW_SCHEMA`, default `raw`) --
   never truncating or touching any other raw table -- reconciles three
   independent counts (page metadata vs. decompressed file, sum of
   pages vs. manifest total, loaded rows vs. manifest total), and
   commits a new per-`(source, endpoint)` high-water mark. All of that
   either commits together or the whole transaction rolls back and the
   prior high-water mark is left untouched. Every row is stored as a
   single `raw_row jsonb` column plus fixed metadata columns -- no type
   coercion, renaming, or business logic happens here. Loading is
   additive (`INSERT ... ON CONFLICT DO NOTHING`, never `TRUNCATE`) and
   safe to repeat for the same `run_id` (idempotent: no duplicate rows).
3. **Sync** (`tuva-ingest sync --endpoint <name> [--since <date>]`) --
   resolves the endpoint's last committed high-water mark (or an
   explicit `--since` override -- see "High-water mark semantics"
   below), then `extract` then `load`, for one endpoint, in a single
   command. Stops immediately (nonzero exit, `load` never attempted,
   watermark untouched) if `extract` fails.
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

**Object storage (production ingestion path)**: `extract`/`load`/`sync
--storage object-storage` (added alongside the filesystem-backed
commands above, default `--storage filesystem` unchanged) publish every
page, then a run manifest, then a success marker as IMMUTABLE objects in
object storage (`OBJECT_STORAGE_PROVIDER=local` for development,
`=s3` for AWS S3/MinIO in production -- see
`src/tuva_ingest/object_storage/`) -- the durable, replayable source of
truth. `load`/`sync` independently re-verify every page's checksum/gzip
integrity/record count from object storage (never trusting a
filesystem-only copy), then COPY accepted rows into a temp table and
merge them into `RAW_SCHEMA` (default `raw_incoming`) with
`ON CONFLICT DO NOTHING` against a source-stable uniqueness rule, all in
one transaction alongside a `(vendor, endpoint)` cursor advance
(`ops.ingestion_cursor`) -- see `docs/SOURCE_CONTRACT.md` "Object
storage" for the complete contract (object-key convention, raw metadata
columns, rejected-record handling, schema-drift observation, cursor
concurrency).

**Secret manager**: `extract`/`sync` never read an API token from an
environment variable by default in a production deployment --
`src/tuva_ingest/secrets.py` retrieves one credential per process from
a configured provider (`TUVA_API_SECRET_PROVIDER`): `env` (default,
reads `TUVA_API_TOKEN` directly -- unchanged local-dev behavior) or
`aws` (AWS Secrets Manager, via `boto3`, using only ambient IAM
identity -- an attached role, instance profile, or local profile --
never a static access key in `.env`). The credential is retrieved at
most once per run, never written to disk, and never logged. See
"Secret manager setup" below for the expected secret JSON shape.

Source data is never loaded directly into any Tuva-managed schema.

**Legacy full-pipeline commands** (`tuva-ingest run`, and
`tuva-ingest load-raw`, which `run` calls internally) still fetch and
load all three raw tables from a single manifest in one call each,
using the older CSV-manifest contract -- kept, documented, and tested
for backward compatibility (see "Backward compatibility" below). The
`extract`/`load`/`sync` commands above are the current, paginated,
immutable, endpoint-scoped way to operate this connector.

## Prerequisites

- [uv](https://docs.astral.sh/uv/) (Python 3.12, pinned via
  `.python-version`)
- Docker + Docker Compose v2, for a local disposable PostgreSQL (or
  point `PG_DSN` at any PostgreSQL 16+ instance you already have)
- Network access to dbt Hub (for `dbt deps`, which fetches the pinned
  Tuva package) when you actually run dbt
- AWS credentials (an attached IAM role, instance profile, or local
  profile -- never a static access key committed to `.env`) only if you
  set `TUVA_API_SECRET_PROVIDER=aws`; the default, `env`, needs no cloud
  account and reads `TUVA_API_TOKEN` directly, same as before

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
| Cloud secret manager (optional, `TUVA_API_SECRET_PROVIDER=aws`) | [`boto3`](https://boto3.amazonaws.com/) -- imported lazily only when the `aws` provider is selected; ambient IAM identity only, never static keys |

`requests` (and its `types-requests` type-stub dependency) have been
fully removed -- `httpx` is now the only HTTP client dependency.

## Quick start

```bash
git clone <this repo> && cd tuva-postgres
make init                      # uv sync --locked; installs pre-commit; copies .env template
# edit .env: at minimum PG_DSN, and TUVA_API_MANIFEST_URL
# if you plan to run `extract`/`sync` against a real paginated API endpoint --
# either TUVA_API_TOKEN (TUVA_API_SECRET_PROVIDER=env, the default), or
# TUVA_API_SECRET_PROVIDER=aws + TUVA_API_SECRET_ID + AWS_REGION (see
# "Secret manager setup" below)

make local-db-ready            # starts a local disposable Postgres (docker compose) and migrates it
make migrate-status            # read-only: confirm 001-005 are applied

# Paginated extract -> load (the current, recommended workflow):
uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
# -> prints {"event": "extract", "run_id": "medical-claims-...", "endpoint": "medical-claims", "pages": 3, ...}
uv run tuva-ingest load --run-id medical-claims-...   # the run_id extract printed
# ...or do both, resolving the endpoint's last committed watermark automatically:
uv run tuva-ingest sync --endpoint medical-claims

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
| `RAW_SCHEMA` | Raw landing schema (never Tuva-managed) | `raw_incoming` (was `raw` -- see "Upgrade notes" in docs/RUNBOOK.md) |
| `OPS_SCHEMA` | Operational/control schema (run/table-load history) | `ops` (was `ingest_ops`) |
| `STAGING_SCHEMA` | Schema dbt materializes `models/staging/*.sql` into | `staging_incoming` |
| `INPUT_LAYER_SCHEMA` | Schema dbt materializes `models/final/*.sql` into | `input_layer` |
| `ANALYTICS_CORE_SCHEMA` | Schema the pinned Tuva package's own core/terminology models route into | `analytics_core` |
| `ANALYTICS_MARTS_SCHEMA` | Schema the pinned Tuva package's own mart models route into | `analytics_marts` |
| `OBJECT_STORAGE_PROVIDER` | `local` or `s3` (see `src/tuva_ingest/object_storage/`) | `local` |
| `OBJECT_STORAGE_BUCKET` | S3(-compatible) bucket name (required for `s3`) | *(none)* |
| `OBJECT_STORAGE_PREFIX` | Object-key prefix (see docs/SOURCE_CONTRACT.md "Object storage") | `raw` |
| `OBJECT_STORAGE_REGION` | AWS region (optional) | *(none)* |
| `OBJECT_STORAGE_ENDPOINT_URL` | Custom S3-compatible endpoint (e.g. MinIO) | *(none)* |
| `INGEST_ROLE` / `TRANSFORM_ROLE` | Least-privilege role names (see `migrations/003_roles_and_grants.sql`) | `tuva_ingest_role` / `tuva_transform_role` |
| `TUVA_API_MANIFEST_URL` | Page-request endpoint (`extract`/`sync`) and legacy manifest endpoint (`run`/`load-raw`'s underlying `extract_snapshot`) | *(required for `extract`/`sync`/`run`)* |
| `TUVA_API_TOKEN` | Bearer token (`SecretStr`); used directly when `TUVA_API_SECRET_PROVIDER=env` (the default) and always by the legacy `run`/`load-raw` path | *(required unless `TUVA_API_SECRET_PROVIDER=aws`)* |
| `TUVA_API_SECRET_PROVIDER` | Credential source for `extract`/`sync`: `env` (reads `TUVA_API_TOKEN` directly) or `aws` (AWS Secrets Manager, ambient IAM identity only) | `env` |
| `TUVA_API_SECRET_ID` | Secret name/ARN in AWS Secrets Manager; required when `TUVA_API_SECRET_PROVIDER=aws`. The secret's JSON body must include an `api_token` key | unset |
| `AWS_REGION` | AWS region used by the `aws` secret provider | unset |
| `TUVA_API_PAGE_SIZE` | Requested page size (`page_size` query parameter); omitted from the request entirely when unset, letting the API apply its own default | unset |
| `TUVA_API_MAX_PAGES` | Hard ceiling on pages fetched by one `extract` run -- a final safety net if the API never signals completion | `10000` |
| `TUVA_API_MAX_PAGE_BYTES` | Max accepted size of one page response body, checked before it is parsed | `67108864` (64 MiB) |
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

## `extract` / `load` / `sync`: secret manager, pagination, immutable files, reconciliation, watermark

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

### Secret manager setup

`extract`/`sync` retrieve exactly one API credential per run through
`src/tuva_ingest/secrets.py`, never a static token baked into a deploy
artifact. `TUVA_API_SECRET_PROVIDER` selects the provider:

* `env` (default) -- reads `TUVA_API_TOKEN` directly. Unchanged local-dev
  and CI behavior; needs no cloud account.
* `aws` -- retrieves the credential from AWS Secrets Manager via `boto3`,
  authenticating with **ambient IAM identity only** (an attached role, an
  instance profile, `AWS_PROFILE`, or `~/.aws/credentials` in local dev).
  This connector never reads, accepts, or configures a static AWS access
  key/secret pair. Requires `TUVA_API_SECRET_ID` (the secret's name or
  ARN) and, optionally, `AWS_REGION`. `boto3` is imported lazily, only
  when this provider is actually selected.

Whichever provider is active, the secret's JSON body (or, for `env`, the
synthesized equivalent) must be shaped:

```json
{"api_token": "<the bearer token>"}
```

Only `api_token` (a non-empty string) is required; unknown extra keys are
ignored. The credential is retrieved at most once per `extract`/`sync`
process -- never once per page -- is never written to disk, and is held
as a `pydantic.SecretStr` so it can never leak through a `repr()`, log
line, or error message.

### Page request contract, immutable files, and run manifest

Each page request sends `endpoint`, `since` (the resolved watermark or an
explicit `--since` override), `page_token` (once past the first page),
and `page_size` (if `TUVA_API_PAGE_SIZE` is set) as real HTTP query
parameters to `TUVA_API_MANIFEST_URL` -- never concatenated into the URL.
The full envelope/pagination/reconciliation/watermark contract this
implements is documented in `docs/SOURCE_CONTRACT.md`; the response shape
`src/tuva_ingest/pagination.py` validates before writing anything to disk
is:

```json
{
  "records": [ {"...": "..."}, {"...": "..."} ],
  "metadata": {
    "record_count": 2,
    "page_token": "<echo of the requested page token, or null on page 1>",
    "next_page_token": "<token for the next page, or null/absent when this is the final page>",
    "high_water_mark": "<opaque, lexicographically-sortable candidate watermark>"
  }
}
```

`extract` requests exactly one page per HTTP call (no prefetching) and
keeps requesting pages only until a validated response's
`next_page_token` is null/absent -- the API's explicit completion
signal. A page token that repeats (either as a request token or a
`next_page_token`) is treated as a pagination cycle and fails loudly;
`TUVA_API_MAX_PAGES` is a final, configurable safety net if the API never
signals completion at all.

Every validated page is written, byte-for-byte unmodified (no renaming,
type coercion, flattening, null-stripping, or reordering), as one JSON
record per line to a gzip-compressed JSONL file, staged first and then
atomically renamed into place, and checksummed (SHA-256) over the exact
stored compressed bytes:

```
RAW_DATA_DIR/
  <source>/
    pages/
      <run_id>/
        page-000001.jsonl.gz
        page-000002.jsonl.gz
        ...
        manifest.json
        _SUCCESS
      .staging/<run_id>-<token>/   (temporary; removed automatically on any failure)
```

`manifest.json` (written and published, `_SUCCESS`-marked, last -- see
`pagination.PaginatedRunStore.finalize`) records every page's file name,
SHA-256, compressed size, record count, request/response/next page
tokens, retrieval timestamp, and candidate high-water mark, plus the
run's `page_count`/`total_record_count`/`candidate_high_water_mark`:

```json
{
  "version": 1,
  "run_id": "medical-claims-20260816T140302-a1b2c3d4e5f6",
  "source": "tuva",
  "endpoint": "medical-claims",
  "since": "2025-01-01",
  "pages": [
    {"page_number": 1, "file_name": "page-000001.jsonl.gz", "sha256": "...", "record_count": 500, "...": "..."}
  ],
  "page_count": 3,
  "total_record_count": 1214,
  "candidate_high_water_mark": "2026-08-16T14:03:00Z",
  "published_at": "2026-08-16T14:03:05.000000Z"
}
```

`load` (and `check_existing_run`) reject any run directory missing its
`_SUCCESS` marker or manifest -- a partially-staged run can never be
loaded. A run is never auto-deleted once published; only its `.staging/`
scratch directory is cleaned up on failure.

### Run identifiers

Unlike the legacy CSV/manifest contract (whose `snapshot_id` is a stable
value the *source* assigns), every `extract` attempt mints a fresh,
non-deterministic `run_id` (`{endpoint}-{utc timestamp}-{random suffix}`)
-- this is deliberate: two `sync --endpoint eligibility` calls an hour
apart, with no explicit `--since`, are each expected to pull whatever is
new since the last committed watermark, and must produce two independent,
separately loadable runs rather than collapsing into a false
"already extracted this" no-op. `PaginatedRunStore.check_existing_run`
still guards the storage layer itself: if a `run_id` ever collides with
an already-published run, that existing run's manifest and every page
checksum are re-verified and reused (a genuine repeat is a safe no-op);
a collision against corrupted or mismatched content fails loudly rather
than silently overwriting anything.

`tuva-ingest load --run-id <value>` resolves the run directly from disk
(`RAW_DATA_DIR/<source>/pages/<run_id>/`) -- no separate database lookup
required -- verifies its `_SUCCESS` marker, then independently
re-checksums and re-counts every page before loading (see
"Reconciliation" below). Repeating `load --run-id <same value>` is a safe,
idempotent no-op (see "Failure recovery and idempotency" below).

### Retry and timeout behavior

`src/tuva_ingest/api_client.py`'s `ApiClient` (a reusable `httpx.Client`)
retries only genuinely transient failures, via `tenacity`, for both page
requests (`get_json_page`) and the legacy manifest fetch:

* httpx connection/timeout errors (`httpx.NetworkError`, `httpx.TimeoutException`)
* HTTP `429`
* HTTP `500`, `502`, `503`, `504`

Ordinary client errors (`400`, `401`, `403`, `404`), envelope validation
failures, cycle-detection failures, and checksum failures are **never**
retried. Retries are bounded by `TUVA_API_MAX_RETRIES` (never unbounded)
with exponential backoff and jitter, honoring a valid `Retry-After`
header -- but every sleep, whichever source produced it, is capped at
`TUVA_API_MAX_RETRY_DELAY_SECONDS`. Connect/read/write/pool timeouts are
each independently configurable (`TUVA_API_CONNECT_TIMEOUT_SECONDS` etc.,
falling back to `TUVA_API_TIMEOUT_SECONDS`). Redirects are never followed
(`follow_redirects=False`) -- a page-request URL redirecting to an
unexpected host must never silently receive this client's bearer token.
Every page response body is size-checked against `TUVA_API_MAX_PAGE_BYTES`
before it is parsed.

### Reconciliation rules

`load` never trusts the manifest's own numbers blindly. Before touching
the database, `paginated_loader.verify_run_manifest` independently
re-verifies, for every page: its on-disk SHA-256 matches the manifest,
and its decompressed JSONL line count matches the manifest's recorded
`record_count`; then it confirms the sum of every page's `record_count`
equals the manifest's own `total_record_count`. After loading, a third
count -- `SELECT count(*) FROM <raw table> WHERE _snapshot_id = <run_id>`
(always a fresh query, so it reads correctly whether this is the first
load or an idempotent repeat) -- must also equal `total_record_count`.
**Any of the three mismatches fails the entire run**, inside the same
transaction as the load, so nothing partial is ever left visible.

### High-water mark semantics

`ingest_ops.source_watermarks` (`migrations/005_paginated_extraction_state.sql`)
stores one durable, committed high-water mark per `(source, endpoint)`.
`sync` (and a plain `extract --endpoint <name>` with no `--since`)
resolves the endpoint's last committed watermark automatically
(`cli._resolve_since`); an explicit `--since` overrides what is requested
for that one extraction, but never by itself changes the durable value.

Each page's `metadata.high_water_mark` is a candidate; the run's
manifest records the *last page's* value as `candidate_high_water_mark`
(pages are requested and written strictly in order, so the last page's
candidate is deterministically the furthest-forward one the source
reported -- see `docs/SOURCE_CONTRACT.md` "High-water mark" for why this
selection strategy is safe). That candidate is committed as the new
watermark only inside `load`'s single transaction, as the very last write
before `conn.commit()`, immediately after every reconciliation count has
matched and the endpoint's raw table load has succeeded -- so the data
load and the watermark advance become visible atomically together, or
neither does. Before committing, `_run_paginated_load` compares the
candidate against the current committed watermark and refuses
(`WatermarkError`, no commit, transaction rolled back) if the candidate
would move it backward. Any failure at any point -- extraction, page
validation, checksum verification, reconciliation, the database load
itself -- leaves the prior watermark completely untouched.

### JSON output and logging

`extract`/`load`/`sync` each print exactly one JSON object to stdout on
success:

```bash
$ uv run tuva-ingest extract --endpoint medical-claims --since 2025-01-01
{"event": "extract", "run_id": "medical-claims-20260816T140302-a1b2c3d4e5f6", "endpoint": "medical-claims", "table": "medical_claim", "since": "2025-01-01", "status": "succeeded", "path": "data/raw/tuva/pages/medical-claims-20260816T140302-a1b2c3d4e5f6", "page_count": 3, "record_count": 1214, "candidate_high_water_mark": "2026-08-16T14:03:00Z"}

$ uv run tuva-ingest load --run-id medical-claims-20260816T140302-a1b2c3d4e5f6
{"event": "load", "run_id": "medical-claims-20260816T140302-a1b2c3d4e5f6", "endpoint": "medical-claims", "table": "medical_claim", "since": "2025-01-01", "status": "succeeded", "row_count": 1214, "path": "data/raw/tuva/pages/medical-claims-20260816T140302-a1b2c3d4e5f6", "high_water_mark": "2026-08-16T14:03:00Z"}
```

`sync` prints the same shape as `load`, plus `"event": "sync"`. Human-
readable diagnostics (progress, retries) go to structured JSON log lines
(also on stdout, one line per event -- see below) or to stderr for fatal
errors; a caller scripting against `tuva-ingest` should parse only the
final stdout line as the command's result. Every failure -- a secret
retrieval error, an envelope/cycle-detection validation error, an HTTP
failure, a checksum mismatch, a reconciliation mismatch, a backward
watermark movement, a database error -- is a nonzero exit code with a
single sanitized stderr line (`ERROR [<category>]: <message>`); `sync`
never prints a success result after a partial failure (`extract` failing
stops it before `load` is ever attempted, and the watermark is left
untouched).

Every log line is one JSON object (`src/tuva_ingest/logging_utils.py`),
UTC ISO-8601 timestamped, with `event`/`level`/`app_version` always
present and `run_id`/`endpoint`/`stage`/`duration_ms`/`table`/
`error_category`/`error_message` where applicable. The paginated flow
emits its own named lifecycle events -- `secret_retrieved`,
`page_request_started`/`page_request_completed`, `page_validated`,
`page_file_published`, `pagination_completed`, `raw_load_started`/
`raw_load_completed`, `reconciliation_completed`, `watermark_committed`,
`run_succeeded`/`run_failed` -- never logging page bodies, record
contents, or credentials:

```json
{"app_version": "0.1.0", "endpoint": "medical-claims", "event": "page_file_published", "level": "INFO", "record_count": 500, "run_id": "medical-claims-20260816T140302-a1b2c3d4e5f6", "sha256": "3a7bd3e2...", "timestamp": "2026-08-16T14:03:02.114000Z"}
```

`TUVA_API_TOKEN`/`PG_DSN`/`Authorization` header values, secret-manager
payloads, and record contents are never present in any log line, JSON
result, or exception message (see "Security" below).

## Backward compatibility

`extract`/`load`/`sync` are the current, paginated, endpoint-scoped
commands (as of this connector, they use the paginated/immutable-file
contract described above, replacing their prior CSV-manifest-based
implementation). Every previously existing command still works,
unchanged, and is tested for compatibility (`tests/unit/test_cli.py`'s
`TestBuildParser`, `tests/integration/test_pipeline_integration.py`):

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
`003_roles_and_grants.sql`, `004_endpoint_scoped_ingestion.sql`,
`005_paginated_extraction_state.sql`, and
`006_object_storage_raw_contract.sql` are the only DDL this repository
owns: the raw and operational-control schemas, run/table-load
bookkeeping tables, least-privilege role grants, (004) the
`endpoint`/`requested_since` columns on `ingestion_runs` plus the unique
index on `table_loads (run_id, table_name)` that makes
`tuva-ingest load --run-id ...` safe to repeat, (005) the
`ingest_ops.source_watermarks` table (the durable per-`(source,
endpoint)` high-water mark `load`/`sync` commit into -- see "High-water
mark semantics" above) plus five new nullable metadata columns and a new
unique index (`(_snapshot_id, _source_row_number)`) on each of the three
raw tables, which is what makes the paginated loader's `INSERT ... ON
CONFLICT DO NOTHING` idempotency possible, and (006) the object-storage-
backed workflow's canonical operational model -- five new SINGULAR
tables (`ingestion_run`, `ingestion_page`, `ingestion_cursor`,
`rejected_record`, `schema_observation`; the pre-existing PLURAL
`ingestion_runs`/`table_loads`/`source_watermarks` above are untouched
and remain the legacy/local-filesystem workflows' own tables) plus seven
new nullable raw-metadata columns and a source-stable partial unique
index on each raw table, and least-privilege grants for the new tables
-- see `docs/SOURCE_CONTRACT.md` "Object storage" for the full contract.
They are checksum-tracked
(`src/tuva_ingest/migrations.py`), applied at most once each, and
rerunning `tuva-ingest migrate` against an already-migrated database is
always a true no-op. Dynamic identifiers (schema/role names) use
psql-style `:"name"` substitution, validated against the same shared
identifier policy every other dynamic-SQL call site uses -- see
`migrations.py`'s module docstring for why static SQL alone can't
express this. Migrations are immutable once applied -- 004 and 005 each
only add nullable columns and new indexes/tables; neither rewrites an
earlier migration, and any future change is a new, forward-only,
numbered migration file.

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
in it. `tests/unit/test_secrets.py` covers both `SecretProvider`
implementations (using a fake provider and a mocked `boto3` client --
never a real AWS call) and `retrieve_api_credential`'s validation.
`tests/unit/test_pagination.py` covers envelope validation (every
malformed-envelope/record_count-mismatch/token-mismatch case),
`PaginatedRunStore` (staged-then-atomic publication, checksum
verification, existing-run reuse vs. corruption detection), and
`extract_paginated_run`'s orchestration (multi-page runs, cycle
detection on both request and `next_page_token` values, `TUVA_API_MAX_PAGES`
enforcement, staging cleanup on failure) against a fake `ApiClient` --
no real HTTP or filesystem access outside a temp dir.
`tests/unit/test_paginated_loader.py` covers `verify_run_manifest`'s
database-free reconciliation checks. `tests/unit/test_state.py`'s
`TestGetWatermark`/`TestCommitWatermark` cover watermark read/write SQL
composition with a fake connection.
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
identical row counts (determinism). `TestPaginatedExtractionAgainstRealDatabase`
extends this suite against the same disposable database to prove, with
real transactions: a paginated run loads correctly and reconciles;
repeating `load --run-id <same value>` never duplicates rows; loading
one endpoint never touches another endpoint's raw table; a
reconciliation mismatch or a simulated mid-load failure rolls back the
*entire* transaction, including any watermark write, leaving the prior
committed watermark unchanged; a successful load commits the watermark
and the data together in the same transaction; and a candidate watermark
that would move the endpoint's committed watermark backward is rejected
before anything is written.

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

- **Paginated extraction** (`tuva-ingest extract`): a run is only
  "published" once every requested page has been validated, written,
  and checksummed, the run manifest is complete, and a `_SUCCESS` marker
  is written -- all staged first, then published via a single atomic
  directory rename. A failure partway through (a validation error, an
  HTTP failure, a detected pagination cycle, exceeding
  `TUVA_API_MAX_PAGES`) cleans up its `.staging/` directory and never
  leaves a partial run at the published path. Every `extract` mints a
  fresh `run_id`, so retrying after a failure simply produces a new,
  independent run rather than resuming/overwriting the failed one; if a
  `run_id` were ever to collide with an existing published run,
  `PaginatedRunStore.check_existing_run` re-verifies every page's
  checksum before treating it as reusable, and fails loudly on any
  mismatch rather than silently reusing corrupted or conflicting
  content.
- **Paginated loading** (`tuva-ingest load --run-id ...` / `sync`): the
  one named table receives an additive `INSERT ... ON CONFLICT
  (_snapshot_id, _source_row_number) DO NOTHING` (never `TRUNCATE`,
  since a paginated run is incremental, not a full replacement) inside
  one transaction spanning that table, run/table-load bookkeeping, three
  reconciliation counts, and the watermark commit -- no other raw table
  is ever truncated, queried, or written. Repeating `load --run-id <same
  value>` is safe and deterministic: the `ON CONFLICT` clause (backed by
  migrations/005's unique index) means no row is ever duplicated, the
  loaded-row reconciliation count is always a fresh `COUNT(*)` (so it
  reads correctly on a repeat, not the INSERT's own affected-row count),
  and `state.commit_watermark`'s `ON CONFLICT (source, endpoint) DO
  UPDATE` means re-committing the same already-committed watermark value
  is also a safe no-op. A reconciliation mismatch or a backward-moving
  candidate watermark rolls back the entire transaction -- the raw table
  load, the run bookkeeping, and the watermark are never partially
  committed.
- **Legacy full-manifest extraction/loading** (`tuva-ingest run` /
  `load-raw`): unchanged from before -- a snapshot is only "published"
  once its artifact(s) are downloaded, verified, and a `_SUCCESS` marker
  is written (re-extracting the exact same `snapshot_id` with different
  content is a loud failure, never a silent overwrite); all three raw
  tables are `TRUNCATE`d and reloaded inside one transaction spanning all
  three tables plus run bookkeeping, so retrying never duplicates rows
  and a failure partway through never leaves a partially-loaded snapshot
  visible.
- **Migrations**: checksum-tracked and applied at most once;
  `apply_pending` takes a PostgreSQL advisory lock so concurrent runs
  never race.
- **Run state**: `ingest_ops.ingestion_runs`/`table_loads` (see
  `migrations/002_ingestion_control.sql`, extended by
  `migrations/004_endpoint_scoped_ingestion.sql`) record every run's
  stage, status, endpoint, row counts, and errors. A run can only leave
  `running` exactly once per attempt (see `src/tuva_ingest/state.py`).
- **Watermark state**: `ingest_ops.source_watermarks` (see
  `migrations/005_paginated_extraction_state.sql`) records one durable
  high-water mark per `(source, endpoint)`, advanced only by a fully
  successful `load`/`sync` transaction -- see "High-water mark
  semantics" above.

## Security

- Never commit `.env` or `profiles.yml` (both git-ignored).
- `PG_DSN` and `TUVA_API_TOKEN` are `pydantic.SecretStr` in
  `IngestConfig` -- never a plain `str` -- so `repr()`/`str()`/pydantic
  validation-error output can never accidentally include the real
  value; both are also never logged, printed, or included in any error
  message via `src/tuva_ingest/logging_utils.py`'s
  `sanitize_error`/`sanitize_text` (defense in depth on top of the
  `SecretStr` type itself).
- The `aws` secret provider (`TUVA_API_SECRET_PROVIDER=aws`) uses only
  ambient AWS identity (an attached IAM role, instance profile,
  `AWS_PROFILE`, or local developer profile) -- `src/tuva_ingest/secrets.py`
  never reads, accepts, or configures a static AWS access key/secret pair.
  Whichever provider is active, the resolved credential is wrapped in a
  `pydantic.SecretStr` immediately, retrieved at most once per run, and
  never written to disk.
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
                     endpoints (--endpoint <-> raw table mapping), secrets (credential providers),
                     pagination (paginated extract + immutable page files), paginated_loader
                     (reconciled, idempotent raw load), extract, raw_loader (legacy CSV contract),
                     state, migrations, cli,
                     object_storage/ (StorageBackend interface + local/memory/s3 backends,
                       key construction, immutable publish/verify), object_extract (object-
                       storage-backed extraction), object_raw_loader (COPY-to-temp + merge +
                       cursor-safe load), endpoint_contract (source-record-id/updated-at/
                       payload-hash contract), schema_observation (PHI-free drift detection)
migrations/           001-006: raw + operational schemas, run/table-load control, role grants,
                     endpoint-scoped ingestion metadata, paginated-extraction watermark state
                     + raw-table idempotency indexes, object-storage-backed canonical
                     operational model + raw-metadata columns + source-stable uniqueness
dbt_project.yml, packages.yml, profiles.example.yml, macros/, models/   the dbt project
tests/unit/            database-free tests (including test_input_layer_contract.py's
                        file-level Input Layer contract checks, test_secrets.py,
                        test_pagination.py, test_paginated_loader.py, test_object_storage_*.py,
                        test_endpoint_contract.py, test_schema_observation.py,
                        test_state_object_storage.py, test_object_raw_loader.py)
tests/integration/     tests requiring a disposable PostgreSQL database (test_pipeline_integration.py,
                        test_object_storage_pipeline_integration.py) and the opt-in
                        MinIO-backed test_object_storage_minio_integration.py
tests/fixtures/         small synthetic CSV fixtures used by tests/integration
docs/RUNBOOK.md         day-to-day operational runbook
docs/SOURCE_CONTRACT.md the full operational source contract: auth, secret manager, pagination,
                        immutable files, reconciliation, and watermark semantics
docs/API_MANIFEST.md    the legacy full-manifest CSV contract extract.py/run/load-raw consume
```
