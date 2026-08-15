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
- `README.md` -- local dev quickstart, SQL tooling, `uv` setup.

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

Checks, in order: PostgreSQL connectivity, migration state (nothing
pending, no checksum mismatches), and freshness of the last successful
run against `PIPELINE_MAX_SUCCESS_AGE_HOURS`. Exit `0` when healthy,
`1` otherwise. Never prints `PG_DSN`/tokens. This is also the container's
`HEALTHCHECK` command (`Dockerfile`).

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
migration's DDL + its `schema_migrations` insert commit or roll back
together) and stops before applying any later migration. Fix the
underlying issue (a conflicting object left over from a manual change is
the most common cause), then re-run `tuva-postgres migrate`. **Do not**
edit an already-applied migration's referenced file(s) to "fix" it --
that produces a checksum mismatch, which the runner correctly refuses to
proceed past. Add a new migration instead.

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
