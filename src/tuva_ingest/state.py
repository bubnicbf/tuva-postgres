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
_SOURCE_WATERMARKS = "source_watermarks"


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
    repeat: `run_id` is stable for the lifetime of one extraction attempt
    (see `pagination.extract_paginated_run`'s `_mint_run_id`), so a
    second `load --run-id X` must never fail with a duplicate-primary-key
    error against `ingestion_runs`; it must instead idempotently redo the
    load and leave `ingestion_runs` describing the most recent attempt.
    Contrast with `create_running_run` above, which every *fresh*-run-id
    caller (`run`, legacy `load-raw`, both of which mint a new
    uuid4-based run_id every invocation) still uses -- those never need
    upsert semantics because their run_id is never reused.

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



# --- durable high-water-mark state (migrations/005_paginated_extraction_state.sql) ---


def get_watermark(conn, ops_schema, source, endpoint) -> dict | None:
    """Read the durable, committed high-water mark for one (source,
    endpoint) pair. Returns `None` if no watermark has ever been
    committed for it (a fresh endpoint, or one that has never had a
    fully-successful paginated `load`/`sync`) -- never a default/zero
    value that could be mistaken for a real prior commit."""
    relation = _relation(ops_schema, _SOURCE_WATERMARKS)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT high_water_mark, successful_run_id, committed_at FROM {relation} "
            f"WHERE source = %s AND endpoint = %s",
            (source, endpoint),
        )
        row = cur.fetchone()
    if row is None:
        return None
    high_water_mark, successful_run_id, committed_at = row
    return {"high_water_mark": high_water_mark, "successful_run_id": successful_run_id, "committed_at": committed_at}


def commit_watermark(conn, ops_schema, source, endpoint, *, high_water_mark, successful_run_id) -> None:
    """Advance the durable high-water mark for (source, endpoint) to
    `high_water_mark`/`successful_run_id`. Callers (see
    `paginated_loader`/cli.py's `_run_paginated_load`) must call this as
    the LAST step of the load transaction, immediately before
    `conn.commit()`, and only after every page has been loaded and every
    reconciliation count has matched -- this function itself does not
    re-validate backward movement (see `cli._run_paginated_load`'s
    explicit backward-movement guard, which runs before this is called);
    it unconditionally sets the value it is given. Does not commit --
    the caller's single transaction commit covers this write too, so the
    data load and the watermark advance are atomic together."""
    relation = _relation(ops_schema, _SOURCE_WATERMARKS)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} (source, endpoint, high_water_mark, successful_run_id, committed_at) "
            f"VALUES (%s, %s, %s, %s, %s) "
            f"ON CONFLICT (source, endpoint) DO UPDATE SET "
            f"high_water_mark = EXCLUDED.high_water_mark, successful_run_id = EXCLUDED.successful_run_id, "
            f"committed_at = EXCLUDED.committed_at",
            (source, endpoint, high_water_mark, successful_run_id, datetime.now(timezone.utc)),
        )


# --- canonical object-storage-backed operational model (migrations/007-008) ---
#
# Everything below writes to the five NEW singular tables
# (ingestion_run, ingestion_page, ingestion_cursor, rejected_record,
# schema_observation) that back ONLY the object-storage-backed workflow
# (object_extract.py/object_raw_loader.py) -- never the legacy
# ingestion_runs/table_loads/source_watermarks tables above, and never
# mixed with them.
#
# Two different commit disciplines are used here, mirroring the split
# already established above for the legacy tables:
#
#   * create_ingestion_run / mark_run_published / mark_run_load_started /
#     mark_run_failed each commit their own small write immediately (the
#     same "operational state must survive a crash a moment later"
#     rationale as create_running_run/mark_failed above) -- these run
#     BEFORE or (for mark_run_failed) AFTER the one atomic load
#     transaction, never inside it.
#
#   * lock_cursor_for_update / commit_cursor / insert_ingestion_page /
#     mark_run_committed / insert_rejected_records /
#     upsert_schema_observations NEVER commit -- they are always called
#     from inside object_raw_loader.load_verified_run's single atomic
#     transaction, and the CALLER (see cli._run_object_load) issues the
#     one conn.commit() that makes the raw merge, every operational
#     write below, and the cursor advance all become visible together,
#     or (on any failure) all roll back together. Calling any of these
#     five and then never committing/rolling back the connection is a
#     caller bug -- these functions have no way to protect against that
#     themselves (see docs/SOURCE_CONTRACT.md "COPY and transactional
#     merge").

_INGESTION_RUN = "ingestion_run"
_INGESTION_PAGE = "ingestion_page"
_INGESTION_CURSOR = "ingestion_cursor"
_REJECTED_RECORD = "rejected_record"
_SCHEMA_OBSERVATION = "schema_observation"


def create_ingestion_run(
    conn, ops_schema, *, run_id, vendor, endpoint, load_date, storage_bucket, storage_run_prefix,
    requested_cursor, app_version, environment,
) -> None:
    """Insert the initial `running` ingestion_run row at the START of
    extraction (before any object is published) -- auto-commits
    immediately so the run is observable even if the process crashes
    mid-extraction. Idempotent for a retried `run_id` (ON CONFLICT DO
    UPDATE resets it back to 'running', the same upsert shape
    `upsert_running_run` above already uses for the legacy contract)."""
    relation = _relation(ops_schema, _INGESTION_RUN)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            "(run_id, vendor, endpoint, load_date, storage_bucket, storage_run_prefix, requested_cursor, "
            "status, started_at, app_version, environment) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s, %s, %s) "
            "ON CONFLICT (run_id) DO UPDATE SET "
            "status = 'running', started_at = EXCLUDED.started_at, published_at = NULL, "
            "load_started_at = NULL, committed_at = NULL, failed_at = NULL, finished_at = NULL, "
            "failure_category = NULL, failure_message = NULL",
            (
                run_id, vendor, endpoint, load_date, storage_bucket, storage_run_prefix, requested_cursor,
                datetime.now(timezone.utc), app_version, environment,
            ),
        )
    conn.commit()


def mark_run_published(conn, ops_schema, run_id, *, candidate_cursor, page_count, extracted_count) -> None:
    """Transition a `running` run to `published` -- called once every
    page and the manifest and the success marker are durable in object
    storage (see object_storage/publish.py), still before any PostgreSQL
    load transaction begins. Auto-commits immediately on success.

    Raises `errors.OperationalStateError` -- rolling back this function's
    own single-statement transaction first, so the connection is left
    usable -- if the run was not currently `running` (a zero-row update
    is never treated as success; see that error's own docstring)."""
    relation = _relation(ops_schema, _INGESTION_RUN)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            "status = 'published', published_at = %s, candidate_cursor = %s, page_count = %s, "
            "extracted_count = %s "
            "WHERE run_id = %s AND status = 'running'",
            (datetime.now(timezone.utc), candidate_cursor, page_count, extracted_count, run_id),
        )
        if cur.rowcount != 1:
            from .errors import OperationalStateError

            conn.rollback()
            raise OperationalStateError(
                f"failed to transition ingestion_run {run_id!r} to 'published': expected it to currently be "
                "'running' (0 rows updated) -- either run_id does not exist, or the run has already left "
                "'running' via a concurrent or prior attempt"
            )
    conn.commit()


def mark_run_load_started(conn, ops_schema, run_id) -> None:
    """Transition a `published` run to `loading`, immediately before the
    one atomic load transaction opens. Auto-commits immediately on
    success -- a crash during loading still leaves 'loading' (not
    'published') visible to operators.

    Raises `errors.OperationalStateError` -- rolling back this function's
    own single-statement transaction first -- if the run was not
    currently `published` (e.g. `load --run-id X` was invoked for a run
    still `running`, already `loading` in a concurrent attempt, or
    already terminal). This is the guard that keeps a `committed` run
    from ever silently being reprocessed: reloading an already-committed
    run raises here, before any raw data or cursor state is touched,
    rather than re-running the whole load transaction unnecessarily."""
    relation = _relation(ops_schema, _INGESTION_RUN)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET status = 'loading', load_started_at = %s "
            "WHERE run_id = %s AND status = 'published'",
            (datetime.now(timezone.utc), run_id),
        )
        if cur.rowcount != 1:
            from .errors import OperationalStateError

            conn.rollback()
            raise OperationalStateError(
                f"failed to transition ingestion_run {run_id!r} to 'loading': expected it to currently be "
                "'published' (0 rows updated) -- either run_id does not exist, or the run is not currently "
                "in a loadable state (running/loading/committed/failed)"
            )
    conn.commit()


def mark_run_failed(conn, ops_schema, run_id, *, failure_category, failure_message) -> None:
    """Transition a run to `failed` in a separate, safe auto-committing
    write -- called AFTER the caller has already rolled back the atomic
    load transaction (see object_raw_loader's module docstring, item 14
    of docs/SOURCE_CONTRACT.md's "COPY and transactional merge"). Never
    called from inside the transaction it is reporting the failure of."""
    relation = _relation(ops_schema, _INGESTION_RUN)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            "status = 'failed', failed_at = %s, finished_at = %s, failure_category = %s, "
            "failure_message = %s "
            "WHERE run_id = %s AND status IN ('running', 'published', 'loading')",
            (datetime.now(timezone.utc), datetime.now(timezone.utc), failure_category, failure_message, run_id),
        )
    conn.commit()


def mark_run_committed(
    conn, ops_schema, run_id, *, accepted_count, rejected_count, inserted_count, duplicate_count,
) -> None:
    """Transition a `loading` run to `committed`. NEVER commits OR rolls
    back (see module section docstring) -- must be the last operational
    write before the caller's own `conn.commit()`, alongside
    `commit_cursor`; transaction-boundary ownership belongs entirely to
    the caller (`object_raw_loader.load_verified_run`'s caller,
    `cli._run_object_load`), which rolls back the whole transaction on
    any exception raised here.

    Raises `errors.OperationalStateError` if the run was not currently
    `loading` (0 rows updated) -- a zero-row update here must never be
    treated as success: it would otherwise mean `conn.commit()` makes
    the raw merge and cursor advance durable while `ingestion_run.status`
    itself silently never reached 'committed', which is exactly the
    "committed run reset or overwritten accidentally" failure mode this
    function must prevent (see docs/RUNBOOK.md "Recovery")."""
    relation = _relation(ops_schema, _INGESTION_RUN)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            "status = 'committed', committed_at = %s, finished_at = %s, accepted_count = %s, "
            "rejected_count = %s, inserted_count = %s, duplicate_count = %s "
            "WHERE run_id = %s AND status = 'loading'",
            (
                datetime.now(timezone.utc), datetime.now(timezone.utc), accepted_count, rejected_count,
                inserted_count, duplicate_count, run_id,
            ),
        )
        if cur.rowcount != 1:
            from .errors import OperationalStateError

            raise OperationalStateError(
                f"failed to transition ingestion_run {run_id!r} to 'committed': expected it to currently be "
                "'loading' (0 rows updated) -- refusing to commit the transaction with an inconsistent "
                "ingestion_run status; the caller must roll back"
            )


def insert_ingestion_page(
    conn, ops_schema, *, run_id, page_number, object_key, checksum, compressed_size_bytes,
    source_record_count, accepted_count, rejected_count, request_cursor, response_cursor,
    next_page_cursor, retrieved_at, status,
) -> None:
    """Upsert one page's audit row. NEVER commits. `ON CONFLICT (run_id,
    page_number)` makes retrying the same run's load safe -- a repeated
    load of the same page re-affirms the identical, immutable
    object_key/checksum rather than erroring or duplicating.

    An idempotent retry may only update the MUTABLE verification/load-
    result columns (`accepted_count`, `rejected_count`, `verified_at`,
    `status`) -- the `DO UPDATE ... WHERE` clause below only applies when
    the existing row's immutable identity (`object_key`, `checksum`,
    `source_record_count`) already matches what this call is asserting.
    If a row already exists for `(run_id, page_number)` with DIFFERENT
    immutable identity, the `WHERE` clause excludes it from the update,
    `RETURNING` yields no row, and this function raises
    `errors.OperationalStateError` rather than silently keeping
    whichever value was already on file -- conflicting metadata for an
    existing run/page must fail loudly, never be silently resolved
    either way."""
    relation = _relation(ops_schema, _INGESTION_PAGE)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            "(run_id, page_number, object_key, checksum, compressed_size_bytes, source_record_count, "
            "accepted_count, rejected_count, request_cursor, response_cursor, next_page_cursor, "
            "retrieved_at, verified_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, page_number) DO UPDATE SET "
            "accepted_count = EXCLUDED.accepted_count, rejected_count = EXCLUDED.rejected_count, "
            "verified_at = EXCLUDED.verified_at, status = EXCLUDED.status "
            f"WHERE {relation}.object_key = EXCLUDED.object_key "
            f"AND {relation}.checksum = EXCLUDED.checksum "
            f"AND {relation}.source_record_count = EXCLUDED.source_record_count "
            "RETURNING 1",
            (
                run_id, page_number, object_key, checksum, compressed_size_bytes, source_record_count,
                accepted_count, rejected_count, request_cursor, response_cursor, next_page_cursor,
                retrieved_at, datetime.now(timezone.utc), status,
            ),
        )
        if cur.fetchone() is None:
            from .errors import OperationalStateError

            raise OperationalStateError(
                f"conflicting ingestion_page metadata for (run_id={run_id!r}, page_number={page_number}): "
                f"an existing row has a different object_key, checksum, or source_record_count than this "
                f"call is asserting (object_key={object_key!r}, checksum={checksum!r}, "
                f"source_record_count={source_record_count!r}) -- immutable page identity must never be "
                "silently overwritten"
            )


def lock_cursor_for_update(conn, ops_schema, vendor, endpoint):
    """`SELECT ... FOR UPDATE` the (vendor, endpoint) cursor row, creating
    it first (at NULL/0) if it does not exist yet -- so every load for a
    given (vendor, endpoint) serializes on this one row regardless of
    whether it is the endpoint's first ever load. NEVER commits. Returns
    `(committed_cursor, successful_run_id, lock_version)`. A concurrent
    second run for the same (vendor, endpoint) blocks here until the
    first run's transaction commits or rolls back -- see
    docs/SOURCE_CONTRACT.md "Cursor safety"."""
    relation = _relation(ops_schema, _INGESTION_CURSOR)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} (vendor, endpoint, committed_cursor, lock_version) "
            "VALUES (%s, %s, NULL, 0) ON CONFLICT (vendor, endpoint) DO NOTHING",
            (vendor, endpoint),
        )
        cur.execute(
            f"SELECT committed_cursor, successful_run_id, lock_version FROM {relation} "
            "WHERE vendor = %s AND endpoint = %s FOR UPDATE",
            (vendor, endpoint),
        )
        return cur.fetchone()


def commit_cursor(conn, ops_schema, vendor, endpoint, *, committed_cursor, successful_run_id, expected_lock_version) -> None:
    """Advance the (vendor, endpoint) cursor. NEVER commits. Caller MUST
    have already called `lock_cursor_for_update` in the same transaction
    and MUST have already independently confirmed `committed_cursor`
    does not move backward relative to what that call returned (see
    `errors.CursorError`, raised by object_raw_loader.py's caller-side
    guard, never by this function itself). `expected_lock_version` is an
    optimistic-concurrency belt-and-braces check on top of the row lock
    already held: since this connection holds `FOR UPDATE` on the row
    for the whole transaction, `expected_lock_version` can only ever
    fail to match here if a caller bug re-reads the row on a different
    connection/transaction than the one holding the lock."""
    relation = _relation(ops_schema, _INGESTION_CURSOR)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {relation} SET "
            "committed_cursor = %s, successful_run_id = %s, committed_at = %s, lock_version = lock_version + 1 "
            "WHERE vendor = %s AND endpoint = %s AND lock_version = %s",
            (
                committed_cursor, successful_run_id, datetime.now(timezone.utc), vendor, endpoint,
                expected_lock_version,
            ),
        )
        if cur.rowcount != 1:
            from .errors import CursorError

            raise CursorError(
                f"failed to advance ingestion_cursor for (vendor={vendor!r}, endpoint={endpoint!r}): "
                f"expected lock_version={expected_lock_version} was not matched (0 rows updated) -- "
                "another transaction must have advanced this cursor concurrently, which should be "
                "impossible while this transaction holds the row lock; this indicates a caller bug, "
                "not a legitimate race"
            )


def insert_rejected_records(conn, ops_schema, rows: list[dict]) -> None:
    """Bulk-insert rejected-record rows via `executemany`, retry-safe via
    `ON CONFLICT (run_id, page_number, record_position) DO NOTHING`.
    NEVER commits. `rows` are plain dicts with keys matching
    `rejected_record`'s columns (see object_raw_loader.py's
    RejectedRecordRow); this function never receives or persists a raw
    payload value itself -- only `raw_object_key` (a durable pointer)
    and already-sanitized `detail` text (see endpoint_contract.Rejected)."""
    if not rows:
        return
    relation = _relation(ops_schema, _REJECTED_RECORD)
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {relation} "
            "(run_id, page_number, record_position, reason_code, detail, source_record_id, payload_hash, "
            "raw_object_key) "
            "VALUES (%(run_id)s, %(page_number)s, %(record_position)s, %(reason_code)s, %(detail)s, "
            "%(source_record_id)s, %(payload_hash)s, %(raw_object_key)s) "
            "ON CONFLICT (run_id, page_number, record_position) DO NOTHING",
            rows,
        )


def upsert_schema_observations(
    conn, ops_schema, *, vendor, endpoint, run_id, page_number, observations: dict, fingerprint: str,
) -> None:
    """Idempotently upsert `{field_path: {type_names}}` observations
    (see schema_observation.py). NEVER commits.

    `occurrence_count` must count distinct (run_id, page_number)
    OCCURRENCES of a (vendor, endpoint, field_path, observed_type)
    combination, never distinct CALLS -- replaying the same page (a
    retried `load --run-id X`, or a run reprocessed after a rollback)
    must not inflate it. The durable occurrence identity that
    distinguishes a genuinely new observation from a replay is exactly
    the row's own `last_observed_run_id`/`last_observed_page_number`
    (already persisted from the previous call that touched this row) --
    no new column is needed: the `CASE` below only increments when this
    call's `(run_id, page_number)` differs from what is already on file
    as the row's last-observed occurrence (`IS DISTINCT FROM` so a NULL
    `last_observed_page_number` -- not expected in practice, since every
    caller always supplies a page_number, but handled defensively --
    compares correctly). A path/type never observed before is inserted
    fresh with occurrence_count = 1, unaffected by this logic."""
    if not observations:
        return
    relation = _relation(ops_schema, _SCHEMA_OBSERVATION)
    now = datetime.now(timezone.utc)
    rows = [
        (vendor, endpoint, path, type_name, fingerprint, run_id, page_number, now, run_id, page_number, now)
        for path, type_names in observations.items()
        for type_name in sorted(type_names)
    ]
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO {relation} AS so "
            "(vendor, endpoint, field_path, observed_type, fingerprint, first_observed_run_id, "
            "first_observed_page_number, first_observed_at, last_observed_run_id, "
            "last_observed_page_number, last_observed_at, occurrence_count) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1) "
            "ON CONFLICT (vendor, endpoint, field_path, observed_type) DO UPDATE SET "
            "fingerprint = EXCLUDED.fingerprint, last_observed_run_id = EXCLUDED.last_observed_run_id, "
            "last_observed_page_number = EXCLUDED.last_observed_page_number, "
            "last_observed_at = EXCLUDED.last_observed_at, "
            "occurrence_count = CASE "
            "WHEN so.last_observed_run_id IS DISTINCT FROM EXCLUDED.last_observed_run_id "
            "OR so.last_observed_page_number IS DISTINCT FROM EXCLUDED.last_observed_page_number "
            "THEN so.occurrence_count + 1 "
            "ELSE so.occurrence_count "
            "END",
            rows,
        )


def get_cursor(conn, ops_schema, vendor, endpoint):
    """Read-only (no row lock) lookup of the current committed cursor for
    (vendor, endpoint) -- used by `object_extract.py` to resolve the
    default `--since`/requested cursor for a fresh extraction. Returns
    `None` if this (vendor, endpoint) has never had a committed run."""
    relation = _relation(ops_schema, _INGESTION_CURSOR)
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT committed_cursor, successful_run_id, committed_at FROM {relation} "
            "WHERE vendor = %s AND endpoint = %s",
            (vendor, endpoint),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    committed_cursor, successful_run_id, committed_at = row
    return {"committed_cursor": committed_cursor, "successful_run_id": successful_run_id, "committed_at": committed_at}
