"""`tuva-ingest` command-line entry point (see pyproject.toml
[project.scripts]).

Subcommands:
  extract      fetch + validate a manifest for exactly one --endpoint
               (optionally scoped by --since) and publish a raw snapshot
               for it (see docs/API_MANIFEST.md, endpoints.py)
  load         load a previously extracted run (--run-id, required) --
               the exact snapshot `extract` published -- into that one
               endpoint's raw table only; never touches any other raw
               table. Safe/idempotent to repeat for the same --run-id.
  sync         extract, then load, the same run, for one --endpoint;
               stops immediately (nonzero exit, no load attempted) if
               extraction fails
  load-raw     load-raw *legacy* full-manifest snapshot (all three raw
               tables in one manifest -- the pre-existing `extract`/
               `run` flow with no --endpoint) into the raw schema
               (default: current). Kept as a documented, tested,
               backward-compatible command -- not superseded by `load`,
               which only ever resolves an endpoint-scoped `extract`
               run.
  migrate      apply pending operational migrations, or print status with --status
  dbt          run `dbt` against this project with the connector's vars/target wired in
  run          full legacy pipeline: extract (full manifest, all three
               tables) -> load-raw -> dbt deps -> dbt build
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

JSON output: `extract`/`load`/`sync` each print exactly one JSON object
to stdout on success (fields include at least `event`, `run_id`,
`endpoint`, `since`, `status`, and -- where applicable -- `row_count`/
`path`); every human-readable diagnostic (progress, retries, errors)
goes to stderr or the structured JSON log stream (also stdout, but one
line per structured log event, never mixed into the single-line command
result) -- see logging_utils.py. A caller scripting against `tuva-ingest`
should parse only the final stdout line as the command's result.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

from . import __version__
from .config import ALL_REQUIREMENTS, REQUIRE_API, REQUIRE_DB, REQUIRE_RAW_DATA, IngestConfig
from .errors import CliUsageError, ConnectorError, RawLoadError, RunNotFoundError
from .logging_utils import configure_logging, log_event, sanitize_error

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_host_identity() -> str:
    from . import migrations

    return migrations.run_host_identity()


def _print_json(payload: dict) -> None:
    """Emit exactly one JSON object to stdout -- the single stable,
    machine-readable result of a successful `extract`/`load`/`sync`
    invocation. Never includes a secret (nothing in `payload` is ever
    sourced from config.safe_dict()-excluded fields -- see each caller)."""
    print(json.dumps(payload, sort_keys=True, default=str))


def _validate_endpoint(endpoint: str) -> str:
    """Reject an unknown --endpoint before any HTTP request or SQL
    statement is issued (see endpoints.table_for_endpoint)."""
    from .endpoints import table_for_endpoint

    table_for_endpoint(endpoint)
    return endpoint


def _validate_since(value: str | None) -> str | None:
    """Reject a malformed --since before any HTTP request is issued.
    Only a plain ISO-8601 calendar date (YYYY-MM-DD) is accepted --
    never a datetime, never a relative expression -- matching the
    manifest/API query-parameter contract (see extract.extract_endpoint_snapshot,
    docs/API_MANIFEST.md)."""
    if value is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        raise CliUsageError(f"--since {value!r} is not a valid ISO-8601 date (expected YYYY-MM-DD)") from None
    return value


# --- extract / load / sync (endpoint-scoped) --------------------------------


def _build_api_client(config: IngestConfig, logger, *, run_id: str | None = None):
    from .api_client import ApiClient

    return ApiClient(
        token=config.api_token_value or "",
        timeout=config.httpx_timeout(),
        max_retries=config.api_max_retries,
        max_retry_delay_seconds=config.api_max_retry_delay_seconds,
        logger=logger,
        run_id=run_id,
    )


def _cmd_extract(args: argparse.Namespace) -> int:
    from .extract import extract_endpoint_snapshot

    endpoint = _validate_endpoint(args.endpoint)
    since = _validate_since(args.since)

    config = IngestConfig.load(required=REQUIRE_API | REQUIRE_RAW_DATA)
    logger = configure_logging(config.log_level)

    client = _build_api_client(config, logger)
    try:
        result = extract_endpoint_snapshot(config, client, logger, endpoint=endpoint, since=since)
    finally:
        client.close()

    _print_json(
        {
            "event": "extract",
            "run_id": result.run_id,
            "endpoint": result.endpoint,
            "table": result.table,
            "since": result.since,
            "status": "skipped" if result.skipped else "succeeded",
            "path": str(result.path),
        }
    )
    return 0


def _run_load(run_id: str, *, config: IngestConfig, logger) -> dict:
    """Resolve `run_id` to the exact published, endpoint-scoped extraction
    it names, verify its success marker and checksums, and transactionally
    load only that one endpoint's raw table -- never any other raw table
    (see raw_loader.load_single_endpoint_snapshot). Safe/deterministic to
    call again for the same `run_id` (see state.upsert_running_run/
    state.upsert_table_load_pending).

    Returns a JSON-able result dict on success. Raises a `ConnectorError`
    subclass (`RunNotFoundError`, `RawLoadError`, ...) on any failure --
    callers (both `_cmd_load` and `_cmd_sync`) let `main()` translate that
    into a single sanitized stderr line and exit code 1, so `sync` can
    never report success after a partial failure, and a caller never sees
    a JSON "success" result for a run that actually failed.
    """
    from . import raw_loader, state
    from .db import connect
    from .endpoints import table_for_endpoint
    from .extract import RawSnapshotStore

    store = RawSnapshotStore(config.raw_data_dir, config.source_name)
    if not store.is_published(run_id):
        raise RunNotFoundError(
            f"run_id {run_id!r} does not resolve to a published extraction under "
            f"{store.snapshot_dir(run_id)} (missing _SUCCESS marker) -- run `tuva-ingest extract` first"
        )

    manifest = store.read_manifest(run_id)
    checksums = store.read_checksums(run_id)
    endpoint = manifest.get("_requested_endpoint")
    since = manifest.get("_requested_since")
    if endpoint is None:
        raise RunNotFoundError(
            f"run_id {run_id!r} is a legacy full-manifest snapshot (published by `extract` with no "
            "--endpoint, or by `run`) -- use `tuva-ingest load-raw --snapshot-id ...` for it instead"
        )
    table = table_for_endpoint(endpoint)
    snapshot_dir = store.snapshot_dir(run_id)

    conn = connect(config.pg_dsn_value)
    try:
        state.upsert_running_run(
            conn, config.ops_schema, run_id=run_id, source=config.source_name, snapshot_id=run_id,
            endpoint=endpoint, requested_since=since, environment=config.pipeline_environment,
            app_version=__version__, host=_run_host_identity(),
        )
        table_checksum = checksums.get(table, {})
        state.upsert_table_load_pending(
            conn, config.ops_schema, run_id, table=table,
            expected_sha256=table_checksum.get("sha256", ""), expected_size_bytes=table_checksum.get("size_bytes", 0),
        )

        try:
            row_count = raw_loader.load_single_endpoint_snapshot(conn, config, snapshot_dir, run_id, table, checksums)
        except RawLoadError as exc:
            conn.rollback()
            state.mark_failed(conn, config.ops_schema, run_id, stage="load", error_category="raw_load", error_message=str(exc))
            state.mark_table_load_failed(conn, config.ops_schema, run_id, table, error_message=str(exc))
            raise

        state.mark_table_load_succeeded(
            conn, config.ops_schema, run_id, table, row_count=row_count,
            actual_sha256=table_checksum.get("sha256", ""), actual_size_bytes=table_checksum.get("size_bytes", 0),
        )
        conn.commit()
        state.mark_succeeded(conn, config.ops_schema, run_id, rows_loaded={table: row_count}, tables_loaded=[table])

        log_event(logger, "raw_table_loaded", run_id=run_id, endpoint=endpoint, table=table, row_count=row_count)
        return {
            "event": "load",
            "run_id": run_id,
            "endpoint": endpoint,
            "table": table,
            "since": since,
            "status": "succeeded",
            "row_count": row_count,
            "path": str(snapshot_dir),
        }
    finally:
        conn.close()


def _cmd_load(args: argparse.Namespace) -> int:
    config = IngestConfig.load(required=REQUIRE_DB | REQUIRE_RAW_DATA)
    logger = configure_logging(config.log_level)
    result = _run_load(args.run_id, config=config, logger=logger)
    _print_json(result)
    return 0


def _cmd_sync(args: argparse.Namespace) -> int:
    from .extract import extract_endpoint_snapshot

    endpoint = _validate_endpoint(args.endpoint)
    since = _validate_since(args.since)

    config = IngestConfig.load(required=ALL_REQUIREMENTS)
    logger = configure_logging(config.log_level)

    # Extraction failure must stop the pipeline immediately -- any
    # exception here propagates straight out of _cmd_sync (never
    # caught/swallowed), so main() reports it as a sanitized, nonzero-exit
    # error and `_run_load` below is never reached.
    client = _build_api_client(config, logger)
    try:
        extracted = extract_endpoint_snapshot(config, client, logger, endpoint=endpoint, since=since)
    finally:
        client.close()

    load_result = _run_load(extracted.run_id, config=config, logger=logger)

    log_event(logger, "sync_succeeded", run_id=extracted.run_id, endpoint=endpoint, table=extracted.table)
    _print_json(
        {
            "event": "sync",
            "run_id": extracted.run_id,
            "endpoint": endpoint,
            "table": extracted.table,
            "since": since,
            "status": "succeeded",
            "row_count": load_result.get("row_count"),
            "path": str(extracted.path),
        }
    )
    return 0


# --- legacy full-manifest commands (unchanged behavior) ----------------


def _cmd_load_raw(args: argparse.Namespace) -> int:
    from . import raw_loader, state
    from .db import connect
    from .extract import RawSnapshotStore

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

    conn = connect(config.pg_dsn_value)
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
    conn = connect(config.pg_dsn_value)
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
    env["PG_DSN"] = config.pg_dsn_value or ""
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
    """Full legacy pipeline: extract (full manifest) -> load-raw -> dbt deps -> dbt build."""
    from . import migrations, raw_loader, state
    from .config import ALL_REQUIREMENTS as _ALL_REQUIREMENTS
    from .db import connect
    from .extract import RawSnapshotStore, extract_snapshot

    config = IngestConfig.load(required=_ALL_REQUIREMENTS)
    logger = configure_logging(config.log_level)
    run_id = f"run-{uuid.uuid4().hex[:12]}"

    conn = connect(config.pg_dsn_value)
    try:
        migrations.apply_pending(conn, config)

        state.create_running_run(
            conn, config.ops_schema, run_id=run_id, source=config.source_name, snapshot_id=None,
            environment=config.pipeline_environment, app_version=__version__, host=_run_host_identity(),
        )

        client = _build_api_client(config, logger, run_id=run_id)
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
        env["PG_DSN"] = config.pg_dsn_value or ""
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

    from .endpoints import SUPPORTED_ENDPOINTS

    p_extract = subparsers.add_parser(
        "extract", help="fetch + validate a manifest for one --endpoint and publish a raw snapshot"
    )
    p_extract.add_argument(
        "--endpoint", required=True, choices=SUPPORTED_ENDPOINTS, help="one supported endpoint (see docs/API_MANIFEST.md)"
    )
    p_extract.add_argument("--since", default=None, help="ISO-8601 date (YYYY-MM-DD); optional, passed through to the manifest request")
    p_extract.set_defaults(func=_cmd_extract)

    p_load = subparsers.add_parser(
        "load", help="load a previously extracted run (--run-id) into that one endpoint's raw table only"
    )
    p_load.add_argument("--run-id", required=True, help="the run_id printed by `tuva-ingest extract`")
    p_load.set_defaults(func=_cmd_load)

    p_sync = subparsers.add_parser("sync", help="extract, then load, one --endpoint in a single command")
    p_sync.add_argument("--endpoint", required=True, choices=SUPPORTED_ENDPOINTS)
    p_sync.add_argument("--since", default=None, help="ISO-8601 date (YYYY-MM-DD)")
    p_sync.set_defaults(func=_cmd_sync)

    p_load_raw = subparsers.add_parser(
        "load-raw",
        help="[legacy] load a full-manifest (all three tables) raw snapshot into the raw schema",
    )
    p_load_raw.add_argument("--snapshot-id", default=None, help="defaults to the 'current' published snapshot")
    p_load_raw.set_defaults(func=_cmd_load_raw)

    p_migrate = subparsers.add_parser("migrate", help="apply pending operational migrations")
    p_migrate.add_argument("--status", action="store_true", help="print migration status and exit (read-only)")
    p_migrate.set_defaults(func=_cmd_migrate)

    p_dbt = subparsers.add_parser("dbt", help="run dbt with this connector's target/vars wired in (e.g. `tuva-ingest dbt -- build`)")
    p_dbt.add_argument("dbt_args", nargs=argparse.REMAINDER, help="arguments passed through to `dbt` (e.g. deps, build, test)")
    p_dbt.set_defaults(func=_cmd_dbt)

    p_run = subparsers.add_parser("run", help="[legacy] full pipeline: extract (full manifest), load-raw, dbt deps, dbt build")
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
