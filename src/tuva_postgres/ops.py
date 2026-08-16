"""Operational bookkeeping: pipeline_runs / pipeline_artifacts writes and
reads (see db/migrations/sql/0002_operational_schema/0002_operational_schema.sql).

Every function here commits its own small write immediately, rather than
participating in one long-lived transaction with the rest of the
pipeline -- so operational state (a `running` row, a partial artifact
record, a `failed` status) remains visible in the database even if the
process crashes or is killed a moment later. Never stores API tokens,
DSNs, CSV row data, or authorization headers -- only run/artifact
metadata, and error messages are expected to already be sanitized by the
caller (see logging_utils.sanitize_error).

`ops_schema` is the one piece of dynamic SQL *syntax* every function here
accepts -- everything else (run_id, source, timestamps, status strings,
error messages, JSON payloads, counts, URLs, checksums, ...) is ordinary
*data*, bound through normal `%s` parameters exactly as before. Table
names (`pipeline_runs`, `pipeline_artifacts`) are fixed string literals
owned by this module, never caller-supplied. `_relation()` below
validates `ops_schema` against the shared identifier policy and composes
a safely quoted `"schema"."table"` string *before* any cursor ever sees
it -- an invalid `ops_schema` raises `InvalidIdentifierError` immediately
and never reaches `cursor.execute()` or `conn.commit()`. See `db.
qualified_relation`'s docstring for why this returns a plain validated,
quoted string rather than a `psycopg.sql.Composed` object.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import qualified_relation

_PIPELINE_RUNS = "pipeline_runs"
_PIPELINE_ARTIFACTS = "pipeline_artifacts"


def _relation(ops_schema: str, table: str) -> str:
    """Validate `ops_schema` (and, defensively, `table`, even though
    every call site below only ever passes one of the two fixed literals
    above) and return a safely quoted `"schema"."table"` identifier
    string. Raises before any SQL is built if either component is
    invalid."""
    return qualified_relation(ops_schema, table, schema_label="ops_schema", relation_label="table")


def create_running_run(conn, ops_schema, *, run_id, source, snapshot_id, environment, app_version, host) -> None:
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, source, snapshot_id, environment, app_version, host, started_at, status, current_stage) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', 'starting')",
            (run_id, source, snapshot_id, environment, app_version, host, datetime.now(timezone.utc)),
        )
    conn.commit()


def update_stage(conn, ops_schema, run_id, stage) -> None:
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET current_stage = %s WHERE run_id = %s",
            (stage, run_id),
        )
    conn.commit()


def set_snapshot_id(conn, ops_schema, run_id, snapshot_id) -> None:
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET snapshot_id = %s WHERE run_id = %s",
            (snapshot_id, run_id),
        )
    conn.commit()


def mark_succeeded(
    conn, ops_schema, run_id, *, artifact_count, bytes_downloaded, rows_loaded, tests_passed, tests_failed
) -> None:
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"status = 'succeeded', current_stage = 'done', finished_at = %s, "
            f"artifact_count = %s, bytes_downloaded = %s, rows_loaded = %s, "
            f"tests_passed = %s, tests_failed = %s "
            f"WHERE run_id = %s",
            (
                datetime.now(timezone.utc),
                artifact_count,
                bytes_downloaded,
                json.dumps(rows_loaded) if rows_loaded is not None else None,
                tests_passed,
                tests_failed,
                run_id,
            ),
        )
    conn.commit()


def mark_failed(conn, ops_schema, run_id, *, stage, error_category, error_message) -> None:
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"status = 'failed', current_stage = %s, finished_at = %s, "
            f"error_category = %s, error_message = %s "
            f"WHERE run_id = %s",
            (stage, datetime.now(timezone.utc), error_category, error_message, run_id),
        )
    conn.commit()


def mark_skipped(conn, ops_schema, run_id, *, source, environment, app_version, host, reason) -> None:
    """Used when the pipeline-wide advisory lock could not be acquired.
    Best-effort: does not require holding the lock itself."""
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, source, snapshot_id, environment, app_version, host, started_at, finished_at, "
            f"status, current_stage, error_category, error_message) "
            f"VALUES (%s, %s, NULL, %s, %s, %s, %s, %s, 'skipped', 'lock', 'lock', %s)",
            (
                run_id,
                source,
                environment,
                app_version,
                host,
                datetime.now(timezone.utc),
                datetime.now(timezone.utc),
                reason,
            ),
        )
    conn.commit()


def record_artifact_pending(conn, ops_schema, run_id, *, table, source_url, expected_sha256, expected_size_bytes) -> None:
    relation = _relation(ops_schema, _PIPELINE_ARTIFACTS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, table_name, source_url, expected_sha256, expected_size_bytes) "
            f"VALUES (%s, %s, %s, %s, %s)",
            (run_id, table, source_url, expected_sha256, expected_size_bytes),
        )
    conn.commit()


def update_artifact_download(conn, ops_schema, run_id, table, *, status, actual_sha256=None, actual_size_bytes=None, raw_path=None) -> None:
    relation = _relation(ops_schema, _PIPELINE_ARTIFACTS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"download_status = %s, actual_sha256 = %s, actual_size_bytes = %s, raw_path = %s "
            f"WHERE run_id = %s AND table_name = %s",
            (status, actual_sha256, actual_size_bytes, raw_path, run_id, table),
        )
    conn.commit()


def update_artifact_load(conn, ops_schema, run_id, table, status) -> None:
    relation = _relation(ops_schema, _PIPELINE_ARTIFACTS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET load_status = %s "
            f"WHERE run_id = %s AND table_name = %s",
            (status, run_id, table),
        )
    conn.commit()


def latest_run(conn, ops_schema):
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT run_id, status, started_at, finished_at, current_stage, error_category "
            f"FROM {relation} ORDER BY started_at DESC LIMIT 1"
        )
        return cur.fetchone()


def latest_successful_run(conn, ops_schema):
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT run_id, finished_at, artifact_count, bytes_downloaded, rows_loaded, "
            f"tests_passed, tests_failed "
            f"FROM {relation} WHERE status = 'succeeded' "
            f"ORDER BY finished_at DESC LIMIT 1"
        )
        return cur.fetchone()


def consecutive_failures(conn, ops_schema) -> int:
    """Count failed runs since the most recent successful run (0 if the
    most recent run succeeded or there is no history)."""
    relation = _relation(ops_schema, _PIPELINE_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT status FROM {relation} "
            f"WHERE status IN ('succeeded', 'failed') ORDER BY started_at DESC"
        )
        rows = cur.fetchall()
    count = 0
    for (status_value,) in rows:
        if status_value == "failed":
            count += 1
        else:
            break
    return count
