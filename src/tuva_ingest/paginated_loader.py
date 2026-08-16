"""Load a published paginated run (see `pagination.py`) into its one
endpoint's raw table, idempotently, inside the caller's transaction --
the pagination-contract analogue of `raw_loader.py`, kept separate
because loading semantics fundamentally differ: the legacy CSV contract
TRUNCATEs and fully replaces a raw table every load (a snapshot is a
complete replacement); this contract *appends* incrementally (a
paginated run only ever contains records "since" the prior watermark)
and must therefore be idempotent via INSERT ... ON CONFLICT DO NOTHING,
never TRUNCATE.

Independently re-verifies every page file's checksum and decompressed
record count against the run manifest before loading anything (defense
against on-disk corruption/tampering between extract and load) -- see
`verify_run_manifest`.

Idempotency key: `(_snapshot_id, _source_row_number)` on each raw table
(added by migrations/005_paginated_extraction_state.sql), with
`_snapshot_id` reused to store the paginated run's `run_id` and
`_source_row_number` assigned as a global, run-wide running counter
(continuing across page boundaries) -- see `load_paginated_run`. This
reuses the exact same two columns the legacy CSV contract already
populates (`raw_loader.py`'s `_RAW_COLUMNS`), so both contracts' rows
coexist in the same physical tables without any schema fork.
"""
from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import qualified_relation
from .endpoints import table_for_endpoint
from .errors import RawLoadError, ReconciliationError
from .pagination import PaginatedRunStore, file_sha256

_STAGING_TABLE = "_tuva_ingest_page_staging"


def _relation(raw_schema: str, table: str) -> str:
    return qualified_relation(raw_schema, table, schema_label="raw_schema", relation_label="table")


def _count_jsonl_records(path: Path) -> int:
    count = 0
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                count += 1
    return count


def _iter_jsonl_records(path: Path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def verify_run_manifest(store: PaginatedRunStore, run_id: str) -> dict:
    """Independently re-verify a published run before loading it: every
    page file's SHA-256 checksum and decompressed record count must match
    what the manifest recorded, and the sum of every page's record_count
    must equal the manifest's own `total_record_count`. Raises
    `ReconciliationError` (listing the exact mismatch) on any failure --
    a corrupted or tampered run must never be silently loaded. Callers
    are responsible for first confirming `run_id` is published at all
    (see `extract.RawSnapshotStore`-style `RunNotFoundError` handling in
    cli.py)."""
    manifest = store.read_manifest(run_id)
    run_dir = store.run_dir(run_id)
    summed_record_count = 0
    for page in manifest["pages"]:
        path = run_dir / page["file_name"]
        if not path.is_file():
            raise ReconciliationError(f"run {run_id!r} is missing page file {page['file_name']!r}")

        actual_sha256 = file_sha256(path)
        if actual_sha256 != page["sha256"]:
            raise ReconciliationError(
                f"run {run_id!r} page {page['file_name']!r}: on-disk sha256 {actual_sha256} does not "
                f"match the manifest's recorded {page['sha256']!r}"
            )

        actual_record_count = _count_jsonl_records(path)
        if actual_record_count != page["record_count"]:
            raise ReconciliationError(
                f"run {run_id!r} page {page['file_name']!r}: file contains {actual_record_count} "
                f"record(s) but its manifest metadata recorded record_count={page['record_count']}"
            )
        summed_record_count += page["record_count"]

    if summed_record_count != manifest["total_record_count"]:
        raise ReconciliationError(
            f"run {run_id!r}: sum of page record counts ({summed_record_count}) does not equal the "
            f"manifest's total_record_count ({manifest['total_record_count']})"
        )
    return manifest


def load_paginated_run(conn, config, store: PaginatedRunStore, run_id: str, manifest: dict) -> None:
    """Load every page of `manifest` into `config.raw_schema`'s table for
    `manifest['endpoint']`, inside the caller's existing transaction --
    never commits or rolls back itself, and never touches any raw table
    other than the one endpoint's. Idempotent: repeating this for the
    same `run_id` inserts nothing new (`ON CONFLICT (_snapshot_id,
    _source_row_number) DO NOTHING`) rather than duplicating rows.
    Raises `RawLoadError` if a page's records cannot be staged/copied.
    """
    endpoint = manifest["endpoint"]
    table = table_for_endpoint(endpoint)
    relation = _relation(config.raw_schema, table)
    run_dir = store.run_dir(run_id)
    loaded_at = datetime.now(timezone.utc)

    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {_STAGING_TABLE} ("
            "_snapshot_id text, _source_row_number bigint, _loaded_at timestamptz, raw_row jsonb, "
            "endpoint text, page_number integer, source_page_token text, retrieved_at timestamptz, "
            "file_sha256 text) ON COMMIT DROP"
        )

    offset = 0
    for page in manifest["pages"]:
        page_path = run_dir / page["file_name"]
        page_number = page["page_number"]
        source_page_token = page.get("response_page_token") or page.get("request_page_token")
        retrieved_at = page["retrieved_at"]
        file_sha256 = page["sha256"]

        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {_STAGING_TABLE}")
            try:
                with cur.copy(
                    f"COPY {_STAGING_TABLE} (_snapshot_id, _source_row_number, _loaded_at, raw_row, "
                    "endpoint, page_number, source_page_token, retrieved_at, file_sha256) FROM STDIN"
                ) as copy:
                    for record in _iter_jsonl_records(page_path):
                        offset += 1
                        copy.write_row((
                            run_id, offset, loaded_at, json.dumps(record, default=str),
                            endpoint, page_number, source_page_token, retrieved_at, file_sha256,
                        ))
            except Exception as exc:
                raise RawLoadError(f"{table!r}: failed while staging page {page['file_name']!r}: {exc}") from exc

            cur.execute(
                f"INSERT INTO {relation} "
                "(_snapshot_id, _source_row_number, _loaded_at, raw_row, endpoint, page_number, "
                "source_page_token, retrieved_at, file_sha256) "
                f"SELECT _snapshot_id, _source_row_number, _loaded_at, raw_row, endpoint, page_number, "
                f"source_page_token, retrieved_at, file_sha256 FROM {_STAGING_TABLE} "
                "ON CONFLICT (_snapshot_id, _source_row_number) DO NOTHING"
            )


def loaded_row_count(conn, raw_schema: str, table: str, run_id: str) -> int:
    """The definitive count of rows present in `raw_schema.table` for
    `run_id` -- a fresh `COUNT(*)`, not the INSERT's own affected-row
    count, so it reads correctly whether this is the first load or an
    idempotent repeat (see module docstring)."""
    relation = _relation(raw_schema, table)
    with conn.cursor() as cur:
        cur.execute(f"SELECT count(*) FROM {relation} WHERE _snapshot_id = %s", (run_id,))
        (count,) = cur.fetchone()
    return count
