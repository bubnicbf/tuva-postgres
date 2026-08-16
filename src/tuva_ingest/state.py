"""Operational bookkeeping: ingestion_runs / table_loads writes and reads
(see migrations/002_ingestion_control.sql).

Every function here commits its own small write immediately, rather than
participating in one long-lived transaction with the rest of the
connector -- so operational state (a `running` row, a partial table-load
record, a `failed` status) remains visible in the database even if the
process crashes or is killed a moment later. Never stores API tokens,
DSNs, CSV row data, or authorization headers -- only run/table-load
metadata, and error messages are expected to already be sanitized by the
caller (see logging_utils.sanitize_error).

`ops_schema` is the one piece of dynamic SQL *syntax* every function here
accepts -- everything else (run_id, source, timestamps, status strings,
error messages, JSON payloads, counts, URLs, checksums, ...) is ordinary
*data*, bound through normal `%s` parameters. Table names
(`ingestion_runs`, `table_loads`) are fixed string literals owned by this
module, never caller-supplied. `_relation()` validates `ops_schema`
against the shared identifier policy and composes a safely quoted
`"schema"."table"` string *before* any cursor ever sees it -- an invalid
`ops_schema` raises `InvalidIdentifierError` immediately and never
reaches `cursor.execute()` or `conn.commit()`.

Run status transitions this module enforces at the SQL level (see
migrations/002_ingestion_control.sql's CHECK constraint and the
mark_succeeded/mark_failed WHERE clauses below): a run can only leave
'running' exactly once, via mark_succeeded/mark_failed. Calling either
function for a run that is not currently 'running' (already terminal, or
does not exist) updates zero rows -- callers that need to notice this
should check the row count/re-read the row rather than assume success.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .db import qualified_relation

_INGESTION_RUNS = "ingestion_runs"
_TABLE_LOADS = "table_loads"


def _relation(ops_schema: str, table: str) -> str:
    """Validate `ops_schema` (and, defensively, `table`, even though every
    call site below only ever passes one of the two fixed literals above)
    and return a safely quoted `"schema"."table"` identifier string.
    Raises before any SQL is built if either component is invalid."""
    return qualified_relation(ops_schema, table, schema_label="ops_schema", relation_label="table")


def create_running_run(conn, ops_schema, *, run_id, source, snapshot_id, environment, app_version, host) -> None:
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, source, snapshot_id, environment, app_version, host, started_at, status, current_stage) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', 'starting')",
            (run_id, source, snapshot_id, environment, app_version, host, datetime.now(timezone.utc)),
        )
    conn.commit()


def update_stage(conn, ops_schema, run_id, stage) -> None:
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET current_stage = %s WHERE run_id = %s AND status = 'running'",
            (stage, run_id),
        )
    conn.commit()


def set_snapshot_id(conn, ops_schema, run_id, snapshot_id) -> None:
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET snapshot_id = %s WHERE run_id = %s AND status = 'running'",
            (snapshot_id, run_id),
        )
    conn.commit()


def mark_succeeded(conn, ops_schema, run_id, *, rows_loaded, tables_loaded) -> None:
    """Transition a `running` run to `succeeded`. Only ever updates a row
    that is currently `running` (see module docstring) -- a run that is
    already terminal is never overwritten."""
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"status = 'succeeded', current_stage = 'done', finished_at = %s, "
            f"rows_loaded = %s, tables_loaded = %s "
            f"WHERE run_id = %s AND status = 'running'",
            (
                datetime.now(timezone.utc),
                json.dumps(rows_loaded) if rows_loaded is not None else None,
                tables_loaded,
                run_id,
            ),
        )
    conn.commit()


def mark_failed(conn, ops_schema, run_id, *, stage, error_category, error_message) -> None:
    """Transition a `running` run to `failed`. Only ever updates a row
    that is currently `running` -- calling this for an already-terminal
    run is a safe no-op (zero rows updated), never a silent overwrite of
    a prior success."""
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"status = 'failed', current_stage = %s, finished_at = %s, "
            f"error_category = %s, error_message = %s "
            f"WHERE run_id = %s AND status = 'running'",
            (stage, datetime.now(timezone.utc), error_category, error_message, run_id),
        )
    conn.commit()


def mark_skipped(conn, ops_schema, run_id, *, source, environment, app_version, host, reason) -> None:
    """Used when the ingestion-wide advisory lock could not be acquired.
    Best-effort: does not require holding the lock itself."""
    relation = _relation(ops_schema, _INGESTION_RUNS)
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


def record_table_load_pending(conn, ops_schema, run_id, *, table, expected_sha256, expected_size_bytes) -> None:
    relation = _relation(ops_schema, _TABLE_LOADS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, table_name, expected_sha256, expected_size_bytes, started_at) "
            f"VALUES (%s, %s, %s, %s, %s)",
            (run_id, table, expected_sha256, expected_size_bytes, datetime.now(timezone.utc)),
        )
    conn.commit()


def mark_table_load_succeeded(conn, ops_schema, run_id, table, *, row_count, actual_sha256, actual_size_bytes) -> None:
    relation = _relation(ops_schema, _TABLE_LOADS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            f"load_status = 'succeeded', row_count = %s, actual_sha256 = %s, actual_size_bytes = %s, "
            f"finished_at = %s "
            f"WHERE run_id = %s AND table_name = %s",
            (row_count, actual_sha256, actual_size_bytes, datetime.now(timezone.utc), run_id, table),
        )
    conn.commit()


def mark_table_load_failed(conn, ops_schema, run_id, table, *, error_message) -> None:
    relation = _relation(ops_schema, _TABLE_LOADS)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET load_status = 'failed', error_message = %s, finished_at = %s "
            f"WHERE run_id = %s AND table_name = %s",
            (error_message, datetime.now(timezone.utc), run_id, table),
        )
    conn.commit()


def latest_run(conn, ops_schema):
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT run_id, status, started_at, finished_at, current_stage, error_category "
            f"FROM {relation} ORDER BY started_at DESC LIMIT 1"
        )
        return cur.fetchone()


def latest_successful_run(conn, ops_schema):
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT run_id, snapshot_id, finished_at, rows_loaded, tables_loaded "
            f"FROM {relation} WHERE status = 'succeeded' "
            f"ORDER BY finished_at DESC LIMIT 1"
        )
        return cur.fetchone()


def consecutive_failures(conn, ops_schema) -> int:
    """Count failed runs since the most recent successful run (0 if the
    most recent run succeeded or there is no history)."""
    relation = _relation(ops_schema, _INGESTION_RUNS)
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


def table_load_row_counts(conn, ops_schema, run_id) -> dict:
    """Return {table_name: row_count} for every table_loads row belonging
    to `run_id` that succeeded -- used by the CLI/tests to confirm every
    expected raw table was actually loaded, not just that the run's
    overall status is 'succeeded'."""
    relation = _relation(ops_schema, _TABLE_LOADS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT table_name, row_count FROM {relation} "
            f"WHERE run_id = %s AND load_status = 'succeeded'",
            (run_id,),
        )
        return {table_name: row_count for table_name, row_count in cur.fetchall()}


def upsert_running_run(
    conn, ops_schema, *, run_id, source, snapshot_id, endpoint=None, requested_since=None,
    environment, app_version, host,
) -> None:
    """Idempotently (re)start a run keyed by `run_id`: insert a fresh
    'running' row, or -- if this exact `run_id` already exists from a
    prior `extract`/`load`/`sync` attempt -- reset it back to 'running'
    in place (`ON CONFLICT (run_id) DO UPDATE`).

    This is what makes `tuva-ingest load --run-id X` (and `sync`, which
    calls it with the same `run_id` `extract` just produced) safe to
    repeat: `run_id` is stable (this connector reuses the extraction's
    own immutable `snapshot_id` as its run id -- see
    `extract.EndpointExtractResult`), so a second `load --run-id X` must
    never fail with a duplicate-primary-key error against
    `ingestion_runs`; it must instead idempotently redo the load and
    leave `ingestion_runs` describing the most recent attempt. Contrast
    with `create_running_run` above, which every *fresh*-run-id caller
    (`run`, legacy `load-raw`, both of which mint a new uuid4-based
    run_id every invocation) still uses -- those never need upsert
    semantics because their run_id is never reused.

    Requires migrations/004_endpoint_scoped_ingestion.sql (the `endpoint`/
    `requested_since` columns on `ingestion_runs`).
    """
    relation = _relation(ops_schema, _INGESTION_RUNS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, source, snapshot_id, endpoint, requested_since, environment, app_version, host, "
            f"started_at, status, current_stage) "
            f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'running', 'starting') "
            f"ON CONFLICT (run_id) DO UPDATE SET "
            f"source = EXCLUDED.source, snapshot_id = EXCLUDED.snapshot_id, "
            f"endpoint = EXCLUDED.endpoint, requested_since = EXCLUDED.requested_since, "
            f"environment = EXCLUDED.environment, app_version = EXCLUDED.app_version, host = EXCLUDED.host, "
            f"started_at = EXCLUDED.started_at, finished_at = NULL, status = 'running', "
            f"current_stage = 'starting', error_category = NULL, error_message = NULL",
            (
                run_id, source, snapshot_id, endpoint, requested_since, environment, app_version, host,
                datetime.now(timezone.utc),
            ),
        )
    conn.commit()


def upsert_table_load_pending(conn, ops_schema, run_id, *, table, expected_sha256, expected_size_bytes) -> None:
    """The idempotent-reload counterpart to `record_table_load_pending`:
    inserts a fresh `table_loads` row, or resets an existing one (same
    `run_id` + `table_name`) back to `pending` in place. Requires
    migrations/004_endpoint_scoped_ingestion.sql's unique constraint on
    `(run_id, table_name)`."""
    relation = _relation(ops_schema, _TABLE_LOADS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            f"(run_id, table_name, expected_sha256, expected_size_bytes, started_at, load_status) "
            f"VALUES (%s, %s, %s, %s, %s, 'pending') "
            f"ON CONFLICT (run_id, table_name) DO UPDATE SET "
            f"expected_sha256 = EXCLUDED.expected_sha256, expected_size_bytes = EXCLUDED.expected_size_bytes, "
            f"started_at = EXCLUDED.started_at, load_status = 'pending', row_count = NULL, "
            f"actual_sha256 = NULL, actual_size_bytes = NULL, error_message = NULL, finished_at = NULL",
            (run_id, table, expected_sha256, expected_size_bytes, datetime.now(timezone.utc)),
        )
    conn.commit()
