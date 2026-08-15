"""`tuva-postgres` command-line entry point (see pyproject.toml
[project.scripts]).

Subcommands:
  run          full pipeline: fetch -> migrate -> load -> test (orchestrator.run_pipeline)
  fetch        fetch + validate the manifest and publish a raw snapshot only
  migrate      apply pending migrations, or print status with --status
  load         load a raw snapshot (default: current) into Postgres
  test         run the SQL data-quality test suite
  healthcheck  verify DB connectivity, migration state, and run freshness

Every subcommand loads only the PipelineConfig fields it actually needs
(see config.REQUIRE_*), so e.g. `tuva-postgres migrate` never requires
TUVA_API_TOKEN to be set.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import REQUIRE_API, REQUIRE_DB, REQUIRE_RAW_DATA, PipelineConfig
from .errors import PipelineError
from .logging_utils import configure_logging, log_event, sanitize_error

REPO_ROOT = Path(__file__).resolve().parents[2]


def _cmd_run(_args: argparse.Namespace) -> int:
    from .config import ALL_REQUIREMENTS
    from .orchestrator import run_pipeline

    config = PipelineConfig.load(required=ALL_REQUIREMENTS)
    return run_pipeline(config)


def _cmd_fetch(_args: argparse.Namespace) -> int:
    from .api_client import ApiClient
    from .landing import RawLandingLayer
    from .manifest import parse_and_validate

    config = PipelineConfig.load(required=REQUIRE_API | REQUIRE_RAW_DATA)
    logger = configure_logging(config.log_level)

    client = ApiClient(
        token=config.api_token,
        timeout_seconds=config.api_timeout_seconds,
        max_retries=config.api_max_retries,
        logger=logger,
    )
    try:
        manifest_raw = client.fetch_manifest_json(config.api_manifest_url)
        manifest = parse_and_validate(manifest_raw, allow_insecure_http=config.api_allow_insecure_http)
        log_event(logger, "manifest_fetched", snapshot_id=manifest.snapshot_id, artifact_count=len(manifest.artifacts))

        landing = RawLandingLayer(config.raw_data_dir, config.source_name)
        if landing.check_idempotent_or_conflicting(manifest.snapshot_id, manifest_raw):
            print(f"Snapshot {manifest.snapshot_id!r} is already published (identical content); nothing to do.")
            print(str(landing.snapshot_dir(manifest.snapshot_id)))
            return 0

        staging_dir = landing.begin_staging(manifest.snapshot_id)
        checksums = {}
        try:
            for artifact in manifest.artifacts:
                log_event(logger, "artifact_download_started", table=artifact.table)
                result = client.download_artifact(artifact, staging_dir)
                log_event(logger, "artifact_download_completed", table=artifact.table, duration_ms=result.duration_ms)
                checksums[artifact.table] = {"sha256": result.sha256, "size_bytes": result.size_bytes}
        except Exception:
            landing.abort_staging(staging_dir)
            raise

        published = landing.finalize(staging_dir, manifest.snapshot_id, manifest_raw, checksums)
        log_event(logger, "raw_snapshot_published", snapshot_id=manifest.snapshot_id, raw_path=str(published.path))
        print(str(published.path))
        return 0
    finally:
        client.close()


def _cmd_migrate(args: argparse.Namespace) -> int:
    from . import migrations
    from .db import connect

    config = PipelineConfig.load(required=REQUIRE_DB)
    conn = connect(config.pg_dsn)
    try:
        if args.status:
            return migrations._print_status(conn, config)  # noqa: SLF001 - intentional reuse of the CLI helper
        applied = migrations.apply_pending(
            conn, config, baseline_existing=args.baseline_existing,
            logger=lambda v, d, ms: print(f"Applied {v}: {d} ({ms:.1f} ms)"),
        )
        print("No pending migrations. Database is up to date." if not applied else f"Applied {len(applied)} migration(s).")
        return 0
    finally:
        conn.close()


def _cmd_load(args: argparse.Namespace) -> int:
    from .landing import RawLandingLayer

    config = PipelineConfig.load(required=REQUIRE_DB | REQUIRE_RAW_DATA)
    landing = RawLandingLayer(config.raw_data_dir, config.source_name)
    snapshot_id = args.snapshot_id or landing.current_snapshot_id()
    if not snapshot_id:
        print("No snapshot_id given and no 'current' snapshot is published yet.", file=sys.stderr)
        return 1
    raw_dir = landing.snapshot_dir(snapshot_id)
    if not landing.is_published(snapshot_id):
        print(f"Snapshot {snapshot_id!r} at {raw_dir} is not published (missing _SUCCESS marker).", file=sys.stderr)
        return 1

    env = dict(__import__("os").environ)
    env.update({"PG_DSN": config.pg_dsn, "PG_SCHEMA": config.pg_schema, "DATA_DIR": str(raw_dir)})
    result = subprocess.run(
        ["bash", str(REPO_ROOT / "scripts" / "load_to_postgres.sh")], env=env, check=False,
    )
    return result.returncode


def _cmd_test(_args: argparse.Namespace) -> int:
    config = PipelineConfig.load(required=REQUIRE_DB)
    env = dict(__import__("os").environ)
    env.update({
        "PG_DSN": config.pg_dsn,
        "PG_SCHEMA": config.pg_schema,
        "TERMINOLOGY_SCHEMA": config.terminology_schema,
    })
    result = subprocess.run(["bash", str(REPO_ROOT / "scripts" / "run_tests.sh")], env=env, check=False)
    return result.returncode


def _cmd_healthcheck(_args: argparse.Namespace) -> int:
    from .healthcheck import run_healthcheck

    config = PipelineConfig.load(required=REQUIRE_DB)
    result = run_healthcheck(config)
    print(result.render())
    return 0 if result.healthy else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tuva-postgres", description="Tuva Postgres ingestion pipeline CLI")
    parser.add_argument("--version", action="version", version=f"tuva-postgres {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_run = subparsers.add_parser("run", help="run the full pipeline: fetch, migrate, load, test")
    p_run.set_defaults(func=_cmd_run)

    p_fetch = subparsers.add_parser("fetch", help="fetch + validate the manifest and publish a raw snapshot")
    p_fetch.set_defaults(func=_cmd_fetch)

    p_migrate = subparsers.add_parser("migrate", help="apply pending database migrations")
    p_migrate.add_argument("--status", action="store_true", help="print migration status and exit (read-only)")
    p_migrate.add_argument(
        "--baseline-existing", action="store_true",
        help="explicitly allow baselining migration 0001 against a non-empty, pre-existing schema",
    )
    p_migrate.set_defaults(func=_cmd_migrate)

    p_load = subparsers.add_parser("load", help="load a raw snapshot into Postgres")
    p_load.add_argument("--snapshot-id", default=None, help="defaults to the 'current' published snapshot")
    p_load.set_defaults(func=_cmd_load)

    p_test = subparsers.add_parser("test", help="run the SQL data-quality test suite")
    p_test.set_defaults(func=_cmd_test)

    p_health = subparsers.add_parser("healthcheck", help="verify DB connectivity, migrations, and run freshness")
    p_health.set_defaults(func=_cmd_healthcheck)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except PipelineError as exc:
        category, message = sanitize_error(exc)
        print(f"ERROR [{category}]: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
