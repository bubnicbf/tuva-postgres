"""`tuva-ingest` command-line entry point (see pyproject.toml
[project.scripts]).

Subcommands:
  extract      fetch + validate the manifest and publish a raw snapshot
  load-raw     load a published raw snapshot into the raw schema (default: current)
  migrate      apply pending operational migrations, or print status with --status
  dbt          run `dbt` against this project with the connector's vars/target wired in
  run          full pipeline: extract -> load-raw -> dbt deps -> dbt build
               --select tag:input_layer -> dbt build --select tag:dq_structural
               (each stage gates the next; see README.md "Validation order")
  healthcheck  verify DB connectivity, migration state, and run freshness

Every subcommand loads only the IngestConfig fields it actually needs
(see config.REQUIRE_*), so e.g. `tuva-ingest migrate` never requires
TUVA_API_TOKEN to be set.

Exit codes: 0 on success, 1 for any handled `ConnectorError` (a clean,
sanitized, single-line error to stderr -- see logging_utils.sanitize_error),
and whatever a shelled-out `dbt`/`psql` subprocess itself returned for the
`dbt`/`load-raw` commands.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import uuid
from pathlib import Path

from . import __version__
from .config import REQUIRE_API, REQUIRE_DB, REQUIRE_RAW_DATA, IngestConfig
from .errors import ConnectorError
from .logging_utils import configure_logging, log_event, sanitize_error

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_host_identity() -> str:
    from . import migrations

    return migrations.run_host_identity()


def _cmd_extract(_args: argparse.Namespace) -> int:
    from .api_client import ApiClient
    from .extract import extract_snapshot

    config = IngestConfig.load(required=REQUIRE_API | REQUIRE_RAW_DATA)
    logger = configure_logging(config.log_level)

    client = ApiClient(
        token=config.api_token,
        timeout_seconds=config.api_timeout_seconds,
        max_retries=config.api_max_retries,
        logger=logger,
    )
    try:
        result = extract_snapshot(config, client, logger)
        if result.skipped:
            print(f"Snapshot {result.snapshot_id!r} is already published (identical content); nothing to do.")
        print(str(result.path))
        return 0
    finally:
        client.close()


def _cmd_load_raw(args: argparse.Namespace) -> int:
    from . import raw_loader, state
    from .db import connect
    from .extract import RawSnapshotStore
    from .errors import RawLoadError

    config = IngestConfig.load(required=REQUIRE_DB | REQUIRE_RAW_DATA)
    logger = configure_logging(config.log_level)

    store = RawSnapshotStore(config.raw_data_dir, config.source_name)
    snapshot_id = args.snapshot_id or store.current_snapshot_id()
    if not snapshot_id:
        print("No --snapshot-id given and no 'current' snapshot is published yet.", file=sys.stderr)
        return 1
    snapshot_dir = store.snapshot_dir(snapshot_id)
    if not store.is_published(snapshot_id):
        print(f"Snapshot {snapshot_id!r} at {snapshot_dir} is not published (missing _SUCCESS marker).", file=sys.stderr)
        return 1

    checksums = store.read_checksums(snapshot_id)
    run_id = f"load-{snapshot_id}-{uuid.uuid4().hex[:8]}"

    conn = connect(config.pg_dsn)
    try:
        state.create_running_run(
            conn, config.ops_schema, run_id=run_id, source=config.source_name, snapshot_id=snapshot_id,
            environment=config.pipeline_environment, app_version=__version__, host=_run_host_identity(),
        )
        for table in ("eligibility", "medical_claim", "pharmacy_claim"):
            table_checksum = checksums.get(table, {})
            state.record_table_load_pending(
                conn, config.ops_schema, run_id, table=table,
                expected_sha256=table_checksum.get("sha256", ""),
                expected_size_bytes=table_checksum.get("size_bytes", 0),
            )

        try:
            row_counts = raw_loader.load_snapshot(conn, config, snapshot_dir, snapshot_id, checksums)
        except RawLoadError as exc:
            conn.rollback()
            state.mark_failed(conn, config.ops_schema, run_id, stage="load_raw", error_category="raw_load", error_message=str(exc))
            for table in ("eligibility", "medical_claim", "pharmacy_claim"):
                state.mark_table_load_failed(conn, config.ops_schema, run_id, table, error_message=str(exc))
            print(f"ERROR [raw_load]: {exc}", file=sys.stderr)
            return 1

        for table, count in row_counts.items():
            table_checksum = checksums.get(table, {})
            state.mark_table_load_succeeded(
                conn, config.ops_schema, run_id, table, row_count=count,
                actual_sha256=table_checksum.get("sha256", ""), actual_size_bytes=table_checksum.get("size_bytes", 0),
            )
        conn.commit()
        state.mark_succeeded(conn, config.ops_schema, run_id, rows_loaded=row_counts, tables_loaded=list(row_counts))

        log_event(logger, "raw_snapshot_loaded", run_id=run_id, snapshot_id=snapshot_id, **row_counts)
        for table, count in row_counts.items():
            print(f"{table}: {count} row(s)")
        return 0
    finally:
        conn.close()


def _cmd_migrate(args: argparse.Namespace) -> int:
    from . import migrations
    from .db import connect

    config = IngestConfig.load(required=REQUIRE_DB)
    conn = connect(config.pg_dsn)
    try:
        if args.status:
            return migrations.print_status(conn, config)
        applied = migrations.apply_pending(
            conn, config, logger=lambda v, f, ms: print(f"Applied {v}: {f} ({ms:.1f} ms)"),
        )
        print("No pending migrations. Database is up to date." if not applied else f"Applied {len(applied)} migration(s).")
        return 0
    finally:
        conn.close()


def _cmd_dbt(args: argparse.Namespace) -> int:
    config = IngestConfig.load(required=REQUIRE_DB)
    dbt_args = list(args.dbt_args)
    if dbt_args and dbt_args[0] == "--":
        dbt_args = dbt_args[1:]

    env = dict(os.environ)
    env["PG_DSN"] = config.pg_dsn or ""
    env["RAW_SCHEMA"] = config.raw_schema
    env["INPUT_LAYER_SCHEMA"] = config.input_layer_schema

    cmd = [
        "dbt", *dbt_args,
        "--project-dir", str(config.dbt_project_dir),
        "--profiles-dir", str(config.dbt_profiles_dir),
        "--target", config.dbt_target,
        "--vars", f"{{raw_schema: {config.raw_schema}, input_layer_schema: {config.input_layer_schema}}}",
    ]
    result = subprocess.run(cmd, env=env, check=False)
    return result.returncode


def _cmd_run(_args: argparse.Namespace) -> int:
    """Full pipeline: extract -> load-raw -> dbt deps -> dbt build."""
    from . import migrations, raw_loader, state
    from .api_client import ApiClient
    from .config import ALL_REQUIREMENTS
    from .db import connect
    from .errors import RawLoadError
    from .extract import RawSnapshotStore, extract_snapshot

    config = IngestConfig.load(required=ALL_REQUIREMENTS)
    logger = configure_logging(config.log_level)
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    conn = connect(config.pg_dsn)
    try:
        migrations.apply_pending(conn, config)

        state.create_running_run(
            conn, config.ops_schema, run_id=run_id, source=config.source_name, snapshot_id=None,
            environment=config.pipeline_environment, app_version=__version__, host=_run_host_identity(),
        )

        client = ApiClient(token=config.api_token, timeout_seconds=config.api_timeout_seconds, max_retries=config.api_max_retries, logger=logger)
        try:
            state.update_stage(conn, config.ops_schema, run_id, "extract")
            extracted = extract_snapshot(config, client, logger)
        finally:
            client.close()
        state.set_snapshot_id(conn, config.ops_schema, run_id, extracted.snapshot_id)

        store = RawSnapshotStore(config.raw_data_dir, config.source_name)
        checksums = store.read_checksums(extracted.snapshot_id)

        state.update_stage(conn, config.ops_schema, run_id, "load_raw")
        try:
            row_counts = raw_loader.load_snapshot(conn, config, extracted.path, extracted.snapshot_id, checksums)
        except RawLoadError as exc:
            conn.rollback()
            state.mark_failed(conn, config.ops_schema, run_id, stage="load_raw", error_category="raw_load", error_message=str(exc))
            print(f"ERROR [raw_load]: {exc}", file=sys.stderr)
            return 1
        conn.commit()

        state.update_stage(conn, config.ops_schema, run_id, "dbt")
        env = dict(os.environ)
        env["PG_DSN"] = config.pg_dsn or ""
        env["RAW_SCHEMA"] = config.raw_schema
        env["INPUT_LAYER_SCHEMA"] = config.input_layer_schema
        dbt_common = ["--project-dir", str(config.dbt_project_dir), "--profiles-dir", str(config.dbt_profiles_dir), "--target", config.dbt_target]
        dbt_vars = ["--vars", f"{{raw_schema: {config.raw_schema}, input_layer_schema: {config.input_layer_schema}}}"]
        deps_result = subprocess.run(["dbt", "deps", *dbt_common], env=env, check=False)
        if deps_result.returncode != 0:
            state.mark_failed(conn, config.ops_schema, run_id, stage="dbt_deps", error_category="dbt", error_message="dbt deps failed")
            return deps_result.returncode

        # Structural DQ gate (see README.md "Validation order"): build
        # ONLY this connector's own Input Layer models first, then ONLY
        # the pinned Tuva package's structural DQ checks -- each stage
        # must succeed before the next one runs, and a failure at either
        # stage stops the pipeline immediately without attempting
        # logical/analytical DQ or any downstream Tuva mart. This
        # mirrors `make pipeline` (see Makefile's dbt-input-layer/
        # dbt-dq-structural targets) so `tuva-ingest run` and `make
        # pipeline` can never silently diverge in ordering.
        input_layer_result = subprocess.run(
            ["dbt", "build", *dbt_common, *dbt_vars, "--select", "tag:input_layer"],
            env=env, check=False,
        )
        if input_layer_result.returncode != 0:
            state.mark_failed(conn, config.ops_schema, run_id, stage="dbt_input_layer", error_category="dbt", error_message="dbt build --select tag:input_layer failed")
            return input_layer_result.returncode

        dq_structural_result = subprocess.run(
            ["dbt", "build", *dbt_common, *dbt_vars, "--select", "tag:dq_structural"],
            env=env, check=False,
        )
        if dq_structural_result.returncode != 0:
            state.mark_failed(conn, config.ops_schema, run_id, stage="dbt_dq_structural", error_category="dbt", error_message="dbt build --select tag:dq_structural failed")
            return dq_structural_result.returncode

        state.mark_succeeded(conn, config.ops_schema, run_id, rows_loaded=row_counts, tables_loaded=list(row_counts))
        log_event(logger, "pipeline_run_succeeded", run_id=run_id, snapshot_id=extracted.snapshot_id, **row_counts)
        print(f"Pipeline run {run_id} succeeded (snapshot={extracted.snapshot_id}).")
        return 0
    finally:
        conn.close()


def _cmd_healthcheck(_args: argparse.Namespace) -> int:
    from .healthcheck import run_healthcheck

    config = IngestConfig.load(required=REQUIRE_DB)
    result = run_healthcheck(config)
    print(result.render())
    return 0 if result.healthy else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tuva-ingest", description="Tuva raw-to-Input-Layer ingestion connector CLI")
    parser.add_argument("--version", action="version", version=f"tuva-ingest {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_extract = subparsers.add_parser("extract", help="fetch + validate the manifest and publish a raw snapshot")
    p_extract.set_defaults(func=_cmd_extract)

    p_load = subparsers.add_parser("load-raw", help="load a published raw snapshot into the raw schema")
    p_load.add_argument("--snapshot-id", default=None, help="defaults to the 'current' published snapshot")
    p_load.set_defaults(func=_cmd_load_raw)

    p_migrate = subparsers.add_parser("migrate", help="apply pending operational migrations")
    p_migrate.add_argument("--status", action="store_true", help="print migration status and exit (read-only)")
    p_migrate.set_defaults(func=_cmd_migrate)

    p_dbt = subparsers.add_parser("dbt", help="run dbt with this connector's target/vars wired in (e.g. `tuva-ingest dbt -- build`)")
    p_dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed through to `dbt` (e.g. deps, build, test)")
    p_dbt.set_defaults(func=_cmd_dbt)

    p_run = subparsers.add_parser("run", help="full pipeline: extract, load-raw, dbt deps, dbt build")
    p_run.set_defaults(func=_cmd_run)

    p_health = subparsers.add_parser("healthcheck", help="verify DB connectivity, migrations, and run freshness")
    p_health.set_defaults(func=_cmd_healthcheck)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConnectorError as exc:
        category, message = sanitize_error(exc)
        print(f"ERROR [{category}]: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
