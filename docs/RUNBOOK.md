# Operations runbook: tuva-postgres ingestion pipeline

This covers running, operating, and troubleshooting the production
ingestion pipeline (`src/tuva_postgres/`): the authenticated API client,
immutable raw landing layer, database migrations, and the orchestrated
`fetch -> migrate -> load -> test` pipeline run. See also:

- `docs/API_MANIFEST.md` -- the versioned JSON manifest contract the API
  client speaks (vendor-agnostic; production supplies the real host via
  `TUVA_API_MANIFEST_URL`).
- `deploy/kubernetes/README.md` -- what the Kubernetes manifests are and
  are not (nothing is applied by this repository).
- `README.md` -- local dev quickstart, SQL tooling, `uv` setup, and the
  Docker Compose local PostgreSQL workflow (`compose.yaml`, `make
  local-db-*`). **That local stack is a development convenience only --
  it is not applied to, or used by, production.** Everything below this
  point in this runbook is about the real, `deploy/kubernetes/`-based
  production deployment; if you're looking for a disposable local
  database instead, see README.md's "Local PostgreSQL with Docker
  Compose" section and stop reading here.

## Required secrets and configuration

Every setting is an environment variable; see `scripts/setup_env.example`
for the full list with non-secret example values, and
`src/tuva_postgres/config.py` for validation rules. Secrets:

| Variable | Where it lives in production |
|---|---|
| `PG_DSN` | Kubernetes Secret (`deploy/kubernetes/secret.example.yaml` template) |
| `TUVA_API_TOKEN` | Kubernetes Secret |

Never committed anywhere, never logged (see `logging_utils.sanitize_text`/
`sanitize_error` and `PipelineConfig.safe_dict()`), never present in an
exception message. Everything else (schemas, timeouts, log level, the
manifest URL itself) is non-secret and lives in a ConfigMap
(`deploy/kubernetes/configmap.yaml`).

## Initial migration (first deploy to a database)

```bash
make migrate                    # or: uv run tuva-postgres migrate
```

On a brand-new, empty database this applies migration `0001` (the
baseline schema) and `0002` (the operational schema) and is done. On a
database that **already has the managed tables** from before this
pipeline existed (e.g., an older manual load), the migration runner
refuses to silently assume it's the expected schema:

```
schema 'tuva' already contains managed tables but has no migration
history for 0001 -- refusing to silently stamp an unknown existing
database as migrated. Re-run with baseline_existing=True ...
```

Verify the existing schema actually matches what migration `0001`
expects (every managed table present, each with a primary key -- see
`migrations._verify_baseline_compatible`), then:

```bash
uv run tuva-postgres migrate --baseline-existing
```

This records `0001` as applied without re-running DDL against tables
that already exist, then proceeds normally. **Do not use
`--baseline-existing` as a default habit** -- it exists for exactly one
scenario (adopting an existing, already-correct database), not as a way
to skip verification.

Check status any time without applying anything:

```bash
make migration-status           # or: uv run tuva-postgres migrate --status
```

## Database migrations architecture

`db/migrations/` is the **sole authoritative home for deployable DDL**.
No table, view, or constraint definition lives anywhere else in this
repository.

- Each migration is a versioned JSON manifest --
  `db/migrations/{version}_{slug}.json` -- declaring its `version`
  (a numeric string like `"0001"`), a `description`, `vars` (SQL
  identifier placeholders mapped to `PipelineConfig` attribute names,
  e.g. `"schema": "PG_SCHEMA"`), and an ordered `files` list. That
  `files` order is the migration's authoritative execution order --
  `tuva_postgres.migrations.discover()` never relies on filesystem
  traversal order.
- Every migration owns an exclusive directory under
  `db/migrations/sql/{version}_{slug}/` (named after its own manifest
  filename), optionally organized into subdirectories for readability
  (migration 0001 uses `core/`, `views/`, and `terminology/`). A manifest
  may only reference files inside its own version-owned directory; the
  runner rejects references to `db/tables/`, another migration's
  directory, or anything outside the repository (path traversal).
- **Every manifest declares exactly one execution mode**, via a required
  `"execution"` field -- `discover()` rejects a manifest with a missing,
  unknown, or wrong-typed value; the mode is never inferred from a
  migration's filename, content, version, or description.
  - `"one_time"` -- applied at most once. Migrations `0001` and `0002`
    are both `one_time`, and this is the right choice for schema changes:
    creating/altering a table, adding a column, adding a constraint.
  - `"repeatable"` -- applied on first discovery, then transactionally
    reapplied whenever its checksum changes, and skipped otherwise
    (standard checksum-driven semantics -- a changed repeatable migration
    is *pending work*, never rerun unconditionally on every invocation).
    Use this for idempotently-written SQL you want to keep current --
    `CREATE OR REPLACE VIEW`, `CREATE OR REPLACE FUNCTION`, and similar
    -- never a one-off schema change. The runner does not parse or verify
    SQL idempotency itself; write repeatable SQL so reapplying it is safe.
- **Applied migrations are immutable** -- both their SQL and their
  declared execution mode. Once a migration has shipped, its files and
  manifest `files` order must never change, and neither may its
  `"execution"` value. Each migration's checksum is a SHA-256 over its
  ordered files' basenames, byte lengths, and contents (manifest
  metadata, including `execution`, never affects the checksum); the
  runner refuses to proceed -- for *any* pending migration, not just the
  affected one -- if an already-applied `one_time` migration's checksum
  no longer matches, or if any applied migration's execution mode no
  longer matches its history (see "Migration failure handling" below).
  Moving a file without changing its basename or bytes preserves its
  checksum -- this is how migrations 0001 and 0002 were reorganized from
  a flat `db/tables/*.sql` layout into version-owned directories, and
  later had `"execution": "one_time"` added to their manifests, without
  ever invalidating a database that had already applied them.
- **Ordering:** within a single run, all pending `one_time` migrations
  apply first (ascending version), then all pending `repeatable`
  migrations (initial application or reapplication, ascending version)
  -- regardless of how versions happen to interleave. This lets a
  repeatable view or function safely depend on a schema object a pending
  `one_time` migration is about to create.
- Database changes always go into a **new** migration at the next unused
  numeric version. Never edit an existing, applied migration.
- Migrations run transactionally (one migration's DDL + its
  `schema_migrations` insert/update commit or roll back together) and are
  recorded in `{OPS_SCHEMA}.schema_migrations`, which additionally tracks
  `execution` and `execution_count` per migration (added via an additive,
  idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` upgrade the first
  time `apply_pending()` runs against an older database -- pre-existing
  rows are backfilled as `one_time` with `execution_count = 1`, and their
  version/checksum/applied-at/description are never touched).
  `migration-status` (and `status()` generally) is read-only and works
  unmodified against either table shape -- it never runs that upgrade.
  Forward migrations only -- there is no automatic down-migration/
  rollback mechanism (see "Image rollback" below for how this repository
  handles rollback instead).
- SQL data-quality/validation queries (the smoke tests and add-on checks
  `scripts/run_tests.sh` runs after a load) are a **separate concern**
  and live under `db/tests/`, not `db/migrations/` -- they are read-only
  checks, never deployable DDL, and are not part of migration discovery.
  `db/tests/zz_results.sql` is test-harness setup (creates/refreshes the
  `test_results` table and summary views) and is applied exactly once,
  before any validation case runs -- it is never itself treated as a
  validation case. Every other `db/tests/*.sql` file is a validation case,
  executed in deterministic filename order. `scripts/run_tests.sh` is the
  authoritative runner for this suite (`make test` invokes it and requires
  the configured database; `make test-shell` is the database-free
  counterpart that validates migration and SQL-test-runner structure and
  behavior via stubbed `psql`/`python3`, with no real Postgres needed).
  Add new SQL validation tests under `db/tests/`; new deployable DDL
  instead always goes into a new migration, per the walkthrough below.

### CI migration idempotency check

CI runs `scripts/apply_schema.sh` (the real migration entry point) twice
against the same unique, disposable schemas, as its own dedicated step
("Run migration suite twice and verify idempotency") that runs right
after the Postgres readiness check and before `make create-db`/`make
load`/`make test`, on every push, pull request, and scheduled run -- with
no `continue-on-error` and no swallowed failures. The first run applies
every pending migration; the second run must be a true no-op:

- zero migrations applied, and the exact same
  `"No pending migrations. Database is up to date."` message the runner
  always prints for a clean state (never a manually-rerun `one_time`
  migration -- the check goes through `scripts/apply_schema.sh` both
  times, exactly like a real deploy would);
- the complete `schema_migrations` history table unchanged, column for
  column, including `execution_count` (a `one_time` migration's count
  must stay `1`, never re-increment);
- a deterministic catalog fingerprint unchanged -- schemas, tables,
  views, columns, primary/foreign/unique/check constraints, indexes,
  functions, and non-internal triggers (Postgres's own auto-generated
  FK-enforcement triggers are deliberately excluded, since their names
  embed a non-deterministic OID and would otherwise look like drift
  between two identically-created schemas); and
- the existing focused foreign-key catalog (every expected FK present
  exactly once, deferrable, initially deferred) unchanged.

It also confirms the final `--status` output has zero pending migrations,
no checksum/execution-mode mismatches, and that calling `--status` itself
never mutates history. Run it locally with `make test-schema-idempotency`
against a real, **disposable** `PG_DSN` (never production -- it creates
and drops its own uniquely-named temporary schemas and never touches
`tuva`/`tuva_term`/`tuva_ops`); it's excluded from `test-shell`/`test`
because it needs Postgres. The harness's control flow -- does it actually
catch a second run that applies something, drifts history, or drifts the
catalog? -- has separate, database-free regression coverage in
`scripts/tests/test_schema_idempotency_harness_controls.sh` (part of
`make test-shell`), which drives the real harness script against a tiny
stubbed migration set instead of simulating Postgres catalog behavior.
This check is about rerun safety of the *unchanged* migration set; a
changed `repeatable` migration's re-execution is a separate concern
covered by `scripts/tests/test_migration_execution_modes.sh`.

### Adding a new migration

1. Pick the next unused numeric version (check `make migration-status`
   or the highest existing `db/migrations/000N_*.json`), e.g. `0003`.
2. Decide its execution mode: `"one_time"` for a schema change (new
   table, new column, new constraint); `"repeatable"` only for
   idempotently-written SQL you want kept current (`CREATE OR REPLACE
   VIEW`/function). Most new migrations are `one_time`.
3. Create `db/migrations/0003_{slug}.json` with `version: "0003"`, a
   clear `description`, the required `"execution"` field from step 2,
   any `vars` your SQL needs (identifier placeholders only -- see
   `_validate_identifier` in `tuva_postgres/migrations.py`), and an
   ordered `files` list.
4. Add the SQL under `db/migrations/sql/0003_{slug}/`, split into
   multiple files if that helps readability -- list them in
   `files` in dependency-safe order (e.g. a table before a view that
   selects from it). If `"repeatable"`, write the SQL so reapplying it is
   safe (`CREATE OR REPLACE ...`, not a plain `CREATE ...`).
5. Run `make migration-status` to confirm it shows up as pending in the
   right section (one-time vs. repeatable), then `make migrate` (or
   `make create-db`) against a disposable database to apply and verify
   it. For a repeatable migration, edit its SQL and rerun `make migrate`
   to confirm it reapplies and `execution_count` increments; a second,
   unchanged run must show it as current, not reapplied.
6. Add unit test coverage in `tests/unit/test_migrations.py` if the
   change exercises new discovery/checksum/execution-mode behavior, and
   integration coverage in `tests/integration/test_pipeline_integration.py`
   if it changes runtime behavior (e.g. new managed tables). Extend
   `scripts/tests/test_schema_constraint_idempotency.sh` if it adds new
   foreign keys or catalog invariants worth pinning.
7. Never modify `db/migrations/0001_baseline.json`,
   `db/migrations/0002_operational_schema.json`, or any file under their
   version-owned directories -- including their `"execution"` value --
   add `0003` (or the next open version) instead, even for a one-line
   fix. Execution mode is immutable once a migration is applied; changing
   it on an already-applied migration is a hard error the runner refuses
   to proceed past.

## Manual run

```bash
make pipeline                   # or: uv run tuva-postgres run
```

Runs fetch, migrate, load, and the SQL data-quality suite once, in one
process. Requires the full environment (`PG_DSN`, `TUVA_API_MANIFEST_URL`,
`TUVA_API_TOKEN`, `RAW_DATA_DIR`, etc.). Exit code `0` on success, `1` on
any stage failure, `3` if another run already holds the pipeline-wide
PostgreSQL advisory lock (see "Concurrency" below).

Individual stages can also be run in isolation for debugging:

```bash
uv run tuva-postgres fetch      # fetch + validate + publish a raw snapshot only
uv run tuva-postgres load       # load the 'current' (or --snapshot-id) raw snapshot
uv run tuva-postgres test       # run the SQL data-quality suite only
```

## Scheduled run

Production scheduling is the Kubernetes `CronJob` in
`deploy/kubernetes/cronjob.yaml`: daily at 06:00 UTC,
`concurrencyPolicy: Forbid`. It is **not applied by this repository** --
see `deploy/kubernetes/README.md` for how to build/push a real image and
apply the manifests deliberately. Do not additionally rely on GitHub
Actions scheduling for production runs; `.github/workflows/ci.yml`'s
nightly cron is for CI/test freshness, not production ingestion.

### Concurrency

Two independent guards prevent overlapping runs:

1. `concurrencyPolicy: Forbid` at the Kubernetes layer.
2. A PostgreSQL session advisory lock (`PIPELINE_LOCK_KEY`, see
   `src/tuva_postgres/db.py`) taken by the orchestrator itself -- this is
   what protects a manual `tuva-postgres run` from racing a scheduled
   run, or a stray `kubectl create job --from=cronjob/...`. If the lock
   can't be acquired, the run exits `3` and records a `skipped` row in
   `pipeline_runs` (best effort; see `ops.mark_skipped`).

## Healthcheck

```bash
make health                     # or: uv run tuva-postgres healthcheck
```

Checks, in order: PostgreSQL connectivity, migration state (unhealthy on
any one-time checksum mismatch, any execution-mode mismatch, or anything
pending -- including a repeatable migration awaiting its initial
application or a reapplication because its checksum changed), and
freshness of the last successful run against
`PIPELINE_MAX_SUCCESS_AGE_HOURS`. Exit `0` when healthy, `1` otherwise.
Never prints `PG_DSN`/tokens. This is also the container's `HEALTHCHECK`
command (`Dockerfile`).

## Viewing structured logs

Every pipeline run emits one JSON object per line to stdout (see
`src/tuva_postgres/logging_utils.py`). In Kubernetes:

```bash
kubectl logs job/<job-name> | jq 'select(.event == "pipeline_failed")'
kubectl logs job/<job-name> | jq -c '{event, stage, error_category, error_message}'
```

Required event names (used consistently across a run):
`pipeline_started`, `pipeline_lock_acquired`, `manifest_fetched`,
`artifact_download_started`, `artifact_download_completed`,
`raw_snapshot_published`, `migration_started`, `migration_completed`,
`load_started`, `table_loaded`, `tests_completed`, `pipeline_succeeded`,
`pipeline_failed`. No log line ever contains CSV row data or a secret
(defense in depth: `logging_utils.sanitize_text`/`sanitize_error` redact
bearer-token- and DSN-shaped substrings even from library-supplied
messages).

## Querying latest runs

```sql
select run_id, status, current_stage, started_at, finished_at, error_category
from tuva_ops.pipeline_runs
order by started_at desc
limit 10;

-- last successful run and how "fresh" it is
select run_id, finished_at, now() - finished_at as age
from tuva_ops.pipeline_runs
where status = 'succeeded'
order by finished_at desc
limit 1;
```

## Finding failed artifacts

```sql
select run_id, table_name, source_url, download_status, load_status,
       expected_sha256, actual_sha256, expected_size_bytes, actual_size_bytes
from tuva_ops.pipeline_artifacts
where run_id = '<run_id>'
  and (download_status <> 'downloaded' or load_status <> 'loaded');
```

`source_url` never includes credentials (query strings are stripped, see
`orchestrator._url_without_credentials`).

## Retrying a failed snapshot

Re-running `tuva-postgres run` (or waiting for the next scheduled run) is
always safe:

- If the upstream manifest still points at the same `snapshot_id` and its
  content is unchanged, the raw landing layer detects this and reuses the
  already-validated snapshot instead of re-downloading
  (`landing.check_idempotent_or_conflicting`).
- The loader (`scripts/load_to_postgres.sh`) treats a snapshot as a
  complete, replaceable unit (truncate + copy in one transaction), so
  re-loading the same snapshot never duplicates rows.
- A brand-new `run_id` and `pipeline_runs` row are created for every
  attempt, so retry history is fully visible.

If the same `snapshot_id` reappears with **different** content upstream,
the landing layer refuses to overwrite the completed, immutable snapshot
and raises loudly (`LandingError`) -- this must be investigated (a
vendor-side snapshot should never be mutated in place); do not work
around it by manually deleting the raw directory without first
understanding why the content changed.

## Checksum mismatch handling

A declared-vs-actual SHA-256 or byte-count mismatch during download
(`ChecksumError`/`DownloadError`) aborts that artifact's download,
cleans up its partial `.part` file, and fails the run at the `fetch`
stage **before anything is published or loaded** -- the previously
committed snapshot (if any) is left completely untouched. Investigate
upstream before retrying (corrupted transfer vs. a genuinely bad
manifest entry are different problems).

## Migration failure handling

A migration failure aborts within a single transaction (the whole
migration's DDL + its `schema_migrations` insert/update commit or roll
back together) and stops before applying any later migration. Fix the
underlying issue (a conflicting object left over from a manual change is
the most common cause), then re-run `tuva-postgres migrate`. **Do not**
edit an already-applied `one_time` migration's referenced file(s) to "fix"
it, and never change an already-applied migration's `"execution"` value
-- either one produces a mismatch (checksum or execution-mode,
respectively), which the runner correctly refuses to proceed past for
*any* pending migration until resolved. Add a new migration instead. A
`repeatable` migration whose checksum has changed is not a failure at all
-- it is normal pending work, and the next `tuva-postgres migrate` simply
reapplies it.

## Image rollback

Container images are tagged deterministically (see
`deploy/kubernetes/kustomization.yaml`'s `images:` override, which this
repository leaves as a documented placeholder). To roll back:

```bash
kubectl set image cronjob/tuva-postgres-pipeline pipeline=<registry>/tuva-postgres:<previous-tag>
```

Because every run is idempotent and every migration is immutable and
additive, rolling back the image does not require rolling back the
database -- an older pipeline binary will simply see the same (or a
strict subset of) applied migrations as `applied` and continue from
there, as long as the older binary's migration set is a prefix of what's
currently applied. Rolling back past a migration that added a column/table
newer code depends on is not supported by design (migrations are
forward-only); coordinate image and schema versions accordingly.

## Raw snapshot retention

The pipeline **never automatically deletes** raw snapshots
(`src/tuva_postgres/landing.py` has no delete path at all). This is
deliberate: raw snapshots are the audit trail and replay source of truth.
Retention is an operator decision, not a pipeline behavior:

- Monitor free space on the `RAW_DATA_DIR` volume (see "Recommended
  alerts" below).
- When pruning, only ever remove complete, published snapshot directories
  (identified by a present `_SUCCESS` marker) older than your retention
  window, and never the snapshot currently referenced by the `current`
  pointer file.
- Size `deploy/kubernetes/pvc.yaml`'s `storage:` request for your actual
  snapshot size and retention window, growing it (most CSI drivers
  support online expansion) rather than under-provisioning.

## Database backup expectations

This repository does not provision or schedule Postgres backups --
that's the responsibility of whatever manages the target PostgreSQL
instance (a managed cloud database, a separate backup operator, etc.).
What the pipeline itself gives you towards recoverability:

- The raw landing layer is an independent, replayable copy of every
  successfully fetched snapshot -- if the database is restored to an
  older point in time (or rebuilt from scratch), replaying the retained
  raw snapshots through `tuva-postgres load` recovers the loaded state
  without needing to re-fetch from the upstream API.
- Migration history (`tuva_ops.schema_migrations`) and operational run
  history (`tuva_ops.pipeline_runs`/`pipeline_artifacts`) are ordinary
  tables in the same database and are covered by whatever backup policy
  covers the rest of the schema.

Confirm with whoever operates the target Postgres instance that backups
are actually configured; this pipeline does not verify that for you.

## Recommended alerts

**No external alerting system has been deployed or configured by this
repository.** The signals below (structured log events and DB-queryable
state) are what the pipeline *emits*; wiring them into a real alerting
system (Prometheus Alertmanager reading `METRICS_FILE`'s textfile
output, a log-based alert on `pipeline_failed`, a scheduled query against
`tuva_ops.pipeline_runs`, etc.) is a separate, deliberate operational
step for whoever owns the production environment.

Recommended alert conditions:

- **No successful run within the freshness threshold** -- exactly what
  `tuva-postgres healthcheck` / the `tuva_postgres_last_success_timestamp_seconds`
  metric checks; alert if `healthcheck` would report unhealthy, or on
  `now() - (select max(finished_at) from pipeline_runs where status='succeeded')`
  exceeding `PIPELINE_MAX_SUCCESS_AGE_HOURS`.
- **Consecutive failures** -- `ops.consecutive_failures()` /
  `tuva_postgres_consecutive_failures` metric; alert at your chosen
  threshold (e.g. >= 2).
- **Migration failure** -- a `pipeline_failed` log event with
  `stage: "migrate"`, or any `tuva-postgres healthcheck` failure citing a
  checksum mismatch or pending migrations.
- **Checksum mismatch** -- a `pipeline_failed` event with
  `error_category: "checksum"`.
- **Partial or rejected manifest** -- a `pipeline_failed` event with
  `error_category: "manifest"`.
- **Data-quality test failures** -- `tests_completed` events where
  `tests_failed > 0`, or `tuva.v_test_failures` for detail.
- **Raw storage nearing capacity** -- a filesystem/PVC-usage alert on the
  `RAW_DATA_DIR` volume (this pipeline does not self-report disk usage).
- **Pipeline duration exceeding the normal window** -- compare
  `tuva_postgres_last_run_duration_seconds` (or `pipeline_runs.finished_at
  - started_at`) against your observed steady-state baseline; this is
  also what `cronjob.yaml`'s `activeDeadlineSeconds` bounds as a hard
  ceiling.
