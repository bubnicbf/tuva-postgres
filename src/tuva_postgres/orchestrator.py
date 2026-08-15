"""The single-run pipeline orchestrator: `tuva-postgres run`.

Stage order: acquire pipeline-wide advisory lock -> apply pending
migrations (this bootstraps the operational schema itself, see note
below) -> create the `running` operational record -> fetch+validate the
manifest -> download+publish the raw snapshot -> load it into Postgres ->
run data-quality tests -> mark the run succeeded. Any failure at any
stage marks the run failed (or is skipped entirely if the lock could not
be acquired) with a sanitized error and a nonzero process exit; the lock
is always released.

Why migrations run before the `running` record is created: pipeline_runs
and pipeline_artifacts (see
db/migrations/sql/0002_operational_schema/0002_operational_schema.sql)
are themselves created by a migration. On a virgin database there is no
operational schema yet to record a run against, so this orchestrator
applies pending migrations first -- on every run after the first this is
a fast no-op (nothing pending), so in steady state the effective order
matches "lock -> running record -> fetch -> migrate -> load -> test".
The migration stage is still fully logged (migration_started/
migration_completed) and, if it fails, the failure is recorded via
structured logs even though no operational row can exist yet to hold it.

Subprocess calls (the existing load_to_postgres.sh / run_tests.sh shell
scripts) are always invoked as argument arrays, never `shell=True`, and
the environment passed to them is a copy of the current environment with
only the pipeline-relevant variables overridden -- never printed or
logged in full (only individual non-secret fields, e.g. PG_SCHEMA, are
ever included in a log event).
"""
from __future__ import annotations

import csv
import secrets
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__, db, migrations, ops
from .api_client import ApiClient
from .errors import LoadError, PipelineError, TestError
from .landing import RawLandingLayer
from .logging_utils import Stopwatch, configure_logging, log_event, sanitize_error, sanitize_text
from .manifest import MANAGED_TABLES, parse_and_validate

REPO_ROOT = Path(__file__).resolve().parents[2]

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_SKIPPED = 3


class PipelineInterrupted(PipelineError):
    category = "interrupted"


def _new_run_id() -> str:
    return f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def _count_csv_data_rows(path: Path) -> int:
    """Row count excluding the header, using the csv module so embedded
    newlines inside quoted fields are not miscounted."""
    with open(path, "r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        try:
            next(reader)  # header
        except StopIteration:
            return 0
        return sum(1 for _row in reader)


def _scrubbed_subprocess_env(base_env: dict, overrides: dict) -> dict:
    """A copy of `base_env` with `overrides` applied. Never mutates
    `base_env`; callers pass the *current* env plus only the pipeline
    variables the child script needs (PG_DSN/PG_SCHEMA/etc.) so nothing
    unrelated leaks into the child beyond what it already had."""
    env = dict(base_env)
    env.update(overrides)
    return env


@dataclass
class PipelineDeps:
    """Injectable dependencies, defaulting to the real implementations.
    Tests substitute fakes here instead of monkeypatching module
    internals."""

    connect: Callable[[str], Any] = db.connect
    try_advisory_lock: Callable[[Any, int], bool] = db.try_advisory_lock
    advisory_unlock: Callable[[Any, int], None] = db.advisory_unlock
    apply_pending: Callable = migrations.apply_pending
    host_identity: Callable[[], str] = migrations.run_host_identity
    api_client_cls: type = ApiClient
    landing_cls: type = RawLandingLayer
    run_subprocess: Callable = subprocess.run
    ops_mod: Any = ops
    environ: dict = field(default_factory=lambda: __import__("os").environ)


class _SignalGuard:
    """Installs SIGTERM/SIGINT handlers for the duration of a pipeline
    run that raise PipelineInterrupted instead of killing the process
    outright, so the orchestrator's normal failure-handling path (record
    failure, release lock, nonzero exit) runs even on a scheduler-issued
    termination signal."""

    def __init__(self) -> None:
        self._previous: dict[int, Any] = {}

    def __enter__(self) -> "_SignalGuard":
        for sig in (signal.SIGTERM, signal.SIGINT):
            self._previous[sig] = signal.getsignal(sig)
            signal.signal(sig, self._handle)
        return self

    def _handle(self, signum, _frame):
        raise PipelineInterrupted(f"received signal {signum}; stopping the pipeline run cleanly")

    def __exit__(self, *exc_info) -> None:
        for sig, handler in self._previous.items():
            signal.signal(sig, handler)


def run_pipeline(config, *, deps: PipelineDeps | None = None) -> int:
    deps = deps or PipelineDeps()
    logger = configure_logging(config.log_level)
    run_id = _new_run_id()
    environment = config.pipeline_environment
    stage = "starting"

    log_event(logger, "pipeline_started", run_id=run_id, environment=environment, source=config.source_name)

    conn = deps.connect(config.pg_dsn)
    lock_held = False
    try:
        with _SignalGuard():
            lock_held = deps.try_advisory_lock(conn, db.PIPELINE_LOCK_KEY)
            if not lock_held:
                log_event(
                    logger,
                    "pipeline_failed",
                    level=30,
                    run_id=run_id,
                    environment=environment,
                    stage="lock",
                    error_category="lock",
                    error_message="pipeline advisory lock is held by another run",
                )
                try:
                    deps.ops_mod.mark_skipped(
                        conn,
                        config.ops_schema,
                        run_id,
                        source=config.source_name,
                        environment=environment,
                        app_version=__version__,
                        host=deps.host_identity(),
                        reason="pipeline advisory lock held by another run",
                    )
                except Exception:  # noqa: BLE001 - best effort only; ops schema may not exist yet
                    pass
                return EXIT_SKIPPED

            log_event(logger, "pipeline_lock_acquired", run_id=run_id, environment=environment)

            stage = "migrate"
            with Stopwatch() as sw:
                log_event(logger, "migration_started", run_id=run_id, environment=environment, stage=stage)
                applied = deps.apply_pending(
                    conn,
                    config,
                    baseline_existing=False,
                    logger=lambda v, d, ms: log_event(
                        logger, "migration_completed", run_id=run_id, environment=environment,
                        stage=stage, table=None, version=v, description=d, duration_ms=ms,
                    ),
                )
            log_event(
                logger, "migration_completed", run_id=run_id, environment=environment, stage=stage,
                duration_ms=sw.elapsed_ms, applied_count=len(applied),
            )

            deps.ops_mod.create_running_run(
                conn, config.ops_schema,
                run_id=run_id, source=config.source_name, snapshot_id=None,
                environment=environment, app_version=__version__, host=deps.host_identity(),
            )

            # --- fetch + validate + land ------------------------------------
            stage = "fetch"
            deps.ops_mod.update_stage(conn, config.ops_schema, run_id, stage)
            api_client = deps.api_client_cls(
                token=config.api_token,
                timeout_seconds=config.api_timeout_seconds,
                max_retries=config.api_max_retries,
                logger=logger,
                run_id=run_id,
            )
            try:
                manifest_raw = api_client.fetch_manifest_json(config.api_manifest_url)
                manifest = parse_and_validate(manifest_raw, allow_insecure_http=config.api_allow_insecure_http)
                log_event(
                    logger, "manifest_fetched", run_id=run_id, environment=environment, stage=stage,
                    snapshot_id=manifest.snapshot_id, artifact_count=len(manifest.artifacts),
                )
                deps.ops_mod.set_snapshot_id(conn, config.ops_schema, run_id, manifest.snapshot_id)

                landing = deps.landing_cls(config.raw_data_dir, config.source_name)
                reused = landing.check_idempotent_or_conflicting(manifest.snapshot_id, manifest_raw)

                bytes_downloaded = 0
                if reused:
                    raw_dir = landing.snapshot_dir(manifest.snapshot_id)
                    checksums = landing.read_checksums(manifest.snapshot_id)
                    for artifact in manifest.artifacts:
                        entry = checksums.get(artifact.table, {})
                        deps.ops_mod.record_artifact_pending(
                            conn, config.ops_schema, run_id, table=artifact.table,
                            source_url=_url_without_credentials(artifact.url),
                            expected_sha256=artifact.sha256, expected_size_bytes=artifact.size_bytes,
                        )
                        deps.ops_mod.update_artifact_download(
                            conn, config.ops_schema, run_id, artifact.table, status="reused",
                            actual_sha256=entry.get("sha256"), actual_size_bytes=entry.get("size_bytes"),
                            raw_path=str(raw_dir / artifact.filename),
                        )
                        bytes_downloaded += int(entry.get("size_bytes") or 0)
                else:
                    staging_dir = landing.begin_staging(manifest.snapshot_id)
                    checksums: dict[str, dict] = {}
                    try:
                        for artifact in manifest.artifacts:
                            deps.ops_mod.record_artifact_pending(
                                conn, config.ops_schema, run_id, table=artifact.table,
                                source_url=_url_without_credentials(artifact.url),
                                expected_sha256=artifact.sha256, expected_size_bytes=artifact.size_bytes,
                            )
                            log_event(
                                logger, "artifact_download_started", run_id=run_id, environment=environment,
                                stage=stage, table=artifact.table,
                            )
                            try:
                                result = api_client.download_artifact(artifact, staging_dir)
                            except Exception as exc:  # noqa: BLE001
                                deps.ops_mod.update_artifact_download(
                                    conn, config.ops_schema, run_id, artifact.table, status="failed",
                                )
                                raise
                            deps.ops_mod.update_artifact_download(
                                conn, config.ops_schema, run_id, artifact.table, status="downloaded",
                                actual_sha256=result.sha256, actual_size_bytes=result.size_bytes,
                                raw_path=str(result.path),
                            )
                            log_event(
                                logger, "artifact_download_completed", run_id=run_id, environment=environment,
                                stage=stage, table=artifact.table, duration_ms=result.duration_ms,
                                size_bytes=result.size_bytes,
                            )
                            checksums[artifact.table] = {"sha256": result.sha256, "size_bytes": result.size_bytes}
                            bytes_downloaded += result.size_bytes
                    except Exception:
                        landing.abort_staging(staging_dir)
                        raise

                    published = landing.finalize(staging_dir, manifest.snapshot_id, manifest_raw, checksums)
                    raw_dir = published.path
                    log_event(
                        logger, "raw_snapshot_published", run_id=run_id, environment=environment, stage=stage,
                        snapshot_id=manifest.snapshot_id, raw_path=str(raw_dir),
                    )
            finally:
                api_client.close()

            # --- load ---------------------------------------------------------
            stage = "load"
            deps.ops_mod.update_stage(conn, config.ops_schema, run_id, stage)
            log_event(logger, "load_started", run_id=run_id, environment=environment, stage=stage,
                      snapshot_id=manifest.snapshot_id)

            load_env = _scrubbed_subprocess_env(
                deps.environ,
                {"PG_DSN": config.pg_dsn, "PG_SCHEMA": config.pg_schema, "DATA_DIR": str(raw_dir)},
            )
            load_result = deps.run_subprocess(
                ["bash", str(REPO_ROOT / "scripts" / "load_to_postgres.sh")],
                env=load_env, capture_output=True, text=True, check=False,
            )
            if load_result.returncode != 0:
                raise LoadError(
                    f"scripts/load_to_postgres.sh exited {load_result.returncode}: "
                    f"{sanitize_text(_tail(load_result.stderr))}"
                )

            rows_loaded: dict[str, int] = {}
            for table in MANAGED_TABLES:
                csv_path = raw_dir / f"{table}.csv"
                row_count = _count_csv_data_rows(csv_path) if csv_path.is_file() else 0
                rows_loaded[table] = row_count
                deps.ops_mod.update_artifact_load(conn, config.ops_schema, run_id, table, "loaded")
                log_event(
                    logger, "table_loaded", run_id=run_id, environment=environment, stage=stage,
                    table=table, rows=row_count,
                )

            # --- data-quality tests --------------------------------------------
            stage = "test"
            deps.ops_mod.update_stage(conn, config.ops_schema, run_id, stage)
            test_env = _scrubbed_subprocess_env(
                deps.environ,
                {
                    "PG_DSN": config.pg_dsn,
                    "PG_SCHEMA": config.pg_schema,
                    "TERMINOLOGY_SCHEMA": config.terminology_schema,
                    "RUN_ID": run_id,
                },
            )
            test_result = deps.run_subprocess(
                ["bash", str(REPO_ROOT / "scripts" / "run_tests.sh")],
                env=test_env, capture_output=True, text=True, check=False,
            )
            tests_passed, tests_failed = _parse_test_summary(test_result.stdout, run_id)
            if test_result.returncode != 0:
                raise TestError(
                    f"scripts/run_tests.sh exited {test_result.returncode}: "
                    f"{sanitize_text(_tail(test_result.stderr))}"
                )
            log_event(
                logger, "tests_completed", run_id=run_id, environment=environment, stage=stage,
                tests_passed=tests_passed, tests_failed=tests_failed,
            )
            if tests_failed > 0:
                raise TestError(f"{tests_failed} data-quality test(s) failed (see tuva.test_results for detail)")

            # --- success --------------------------------------------------------
            deps.ops_mod.mark_succeeded(
                conn, config.ops_schema, run_id,
                artifact_count=len(manifest.artifacts), bytes_downloaded=bytes_downloaded,
                rows_loaded=rows_loaded, tests_passed=tests_passed, tests_failed=tests_failed,
            )
            log_event(
                logger, "pipeline_succeeded", run_id=run_id, environment=environment,
                snapshot_id=manifest.snapshot_id, artifact_count=len(manifest.artifacts),
                bytes_downloaded=bytes_downloaded, tests_passed=tests_passed,
            )
            return EXIT_SUCCESS

    except Exception as exc:  # noqa: BLE001 - single top-level failure handler by design
        category, message = sanitize_error(exc)
        log_event(
            logger, "pipeline_failed", level=40, run_id=run_id, environment=environment, stage=stage,
            error_category=category, error_message=message,
        )
        try:
            deps.ops_mod.mark_failed(
                conn, config.ops_schema, run_id, stage=stage, error_category=category, error_message=message,
            )
        except Exception:  # noqa: BLE001 - best effort; the run row may not exist if migrate itself failed
            pass
        return EXIT_FAILURE
    finally:
        try:
            if lock_held:
                deps.advisory_unlock(conn, db.PIPELINE_LOCK_KEY)
        finally:
            conn.close()


def _url_without_credentials(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


def _tail(text: str | None, max_chars: int = 2000) -> str:
    if not text:
        return ""
    return text[-max_chars:]


def _parse_test_summary(stdout: str, run_id: str) -> tuple[int, int]:
    for line in (stdout or "").splitlines():
        if line.startswith("summary|"):
            parts = line.split("|")
            if len(parts) == 5:
                _label, rid, passed, failed, _total = parts
                if rid != run_id:
                    continue  # stale/foreign summary line; keep looking, then fail below
                try:
                    return int(passed), int(failed)
                except ValueError:
                    break
    raise TestError("scripts/run_tests.sh did not print a parseable 'summary|<run_id>|pass|fail|total' line for this run")


def main(argv: list[str] | None = None) -> int:
    from .config import ALL_REQUIREMENTS, PipelineConfig

    config = PipelineConfig.load(required=ALL_REQUIREMENTS)
    return run_pipeline(config)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
