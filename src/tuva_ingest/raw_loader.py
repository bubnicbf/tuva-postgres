"""Load extracted source files into the configured raw warehouse schema
only -- never into any Tuva-managed core, terminology, or output schema.

Design
------
Raw fidelity over raw-time typing: every row of every managed raw table
(`eligibility`, `medical_claim`, `pharmacy_claim` -- see
`manifest.RAW_TABLES`) is stored as a single `raw_row jsonb` column,
built directly from that CSV's own header row and values, with no type
coercion, renaming, or filtering at this layer. Tuva-specific
transformations (typing, normalization, null handling, code mapping)
belong in dbt staging models (see models/staging/), not here -- this
keeps the raw layer a faithful, replayable copy of exactly what the
source sent, and means a source schema change (a renamed or added
column) never breaks the raw load itself, only (visibly, in dbt) the
staging model that reads it.

Column names are therefore never derived from untrusted CSV headers --
every raw table has the same fixed, hardcoded column list
(`_RAW_COLUMNS` below): `_snapshot_id`, `_source_row_number`,
`_loaded_at`, `raw_row`. Only `raw_schema` (validated via
`db.qualified_relation`) is dynamic SQL *syntax*; the CSV header/values
that become a row's `raw_row` JSON payload are ordinary *data*, passed
through psycopg's COPY protocol as a JSON-encoded value, never
interpolated into SQL text.

Retry semantics: loading a snapshot TRUNCATEs each raw table before
copying that snapshot's rows in, all inside one transaction alongside
this run's `table_loads` bookkeeping (see state.py) -- so retrying the
same (or a different) snapshot_id always ends in exactly that snapshot's
rows, never a duplicate, and a failure partway through (a bad CSV row, a
lost connection) rolls back every table's TRUNCATE+COPY together: a
partially-loaded snapshot is never left visible to readers.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .db import qualified_relation
from .errors import RawLoadError
from .manifest import RAW_TABLES

# Fixed, hardcoded raw-table column list -- never derived from a CSV
# header or any other untrusted input. Every managed raw table has
# exactly this shape.
_RAW_COLUMNS = ("_snapshot_id", "_source_row_number", "_loaded_at", "raw_row")
_RAW_COLUMNS_SQL = ", ".join(_RAW_COLUMNS)

CHUNK_SIZE = 1024 * 1024


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(CHUNK_SIZE)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def _relation(raw_schema: str, table: str) -> str:
    return qualified_relation(raw_schema, table, schema_label="raw_schema", relation_label="table")


def _iter_csv_rows(csv_path: Path):
    """Yield each data row of `csv_path` as an ordered dict-shaped
    mapping (header -> value), using the csv module (never naive
    comma-splitting) so quoted fields/embedded commas/newlines are
    handled correctly."""
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise RawLoadError(f"{csv_path}: no header row found")
        for row in reader:
            yield row


def verify_file_checksum(csv_path: Path, expected_sha256: str, *, table: str) -> None:
    """Recompute the on-disk file's sha256 and compare it against the
    checksum recorded at extraction time (see extract.py/checksums.json).
    Raises RawLoadError on mismatch -- the raw loader is a separate,
    independently-retryable step from extraction, so this is a
    defense-in-depth check against on-disk corruption/tampering between
    the two, not a duplicate of the download-time verification."""
    actual = _file_sha256(csv_path)
    if actual != expected_sha256:
        raise RawLoadError(
            f"{table!r}: on-disk checksum {actual} does not match the checksum recorded at "
            f"extraction time {expected_sha256} -- refusing to load a file that may have been "
            "corrupted or modified since it was downloaded"
        )


def load_table(conn, raw_schema: str, table: str, csv_path: Path, snapshot_id: str) -> int:
    """TRUNCATE and reload a single raw table from `csv_path`, all inside
    the caller's existing transaction (this function never commits).
    Returns the number of rows loaded. Raises RawLoadError on any row
    that isn't a clean CSV data row; the caller is responsible for
    rolling back the whole transaction if any table's load fails, so a
    partially-loaded snapshot never becomes visible."""
    relation = _relation(raw_schema, table)
    loaded_at = datetime.now(timezone.utc)
    row_count = 0

    with conn.cursor() as cur:
        cur.execute(f"TRUNCATE TABLE {relation}")
        try:
            with cur.copy(f"COPY {relation} ({_RAW_COLUMNS_SQL}) FROM STDIN") as copy:
                for row_number, row in enumerate(_iter_csv_rows(csv_path), start=1):
                    copy.write_row((snapshot_id, row_number, loaded_at, json.dumps(row, default=str)))
                    row_count += 1
        except Exception as exc:
            raise RawLoadError(f"{table!r}: failed while loading row {row_count + 1}: {exc}") from exc

    return row_count


def load_snapshot(conn, config, snapshot_dir: Path, snapshot_id: str, checksums: dict) -> dict[str, int]:
    """Load every managed raw table (`manifest.RAW_TABLES`) from
    `snapshot_dir` into `config.raw_schema`, inside one transaction:
    every table's TRUNCATE+COPY either all succeed and are committed
    together by the caller, or (on any failure) the whole transaction is
    left for the caller to roll back -- a snapshot with only some of its
    tables loaded is never committed.

    `checksums` is the `{table: {"sha256": ..., "size_bytes": ...}}`
    mapping written by extract.py at download time (see
    `checksums.json`); each file's on-disk checksum is re-verified
    before it is loaded (see `verify_file_checksum`).

    Does not commit or roll back the connection itself -- callers own the
    transaction boundary (see cli.py's `_cmd_load_raw`), so this function
    can be composed with state.py's run/table-load bookkeeping in the
    same transaction.
    """
    row_counts: dict[str, int] = {}
    for table in RAW_TABLES:
        csv_path = snapshot_dir / f"{table}.csv"
        if not csv_path.is_file():
            raise RawLoadError(f"expected raw file not found for managed table {table!r}: {csv_path}")

        table_checksum = checksums.get(table)
        if not table_checksum or "sha256" not in table_checksum:
            raise RawLoadError(f"no recorded checksum for managed table {table!r} in this snapshot's checksums.json")
        verify_file_checksum(csv_path, table_checksum["sha256"], table=table)

        row_counts[table] = load_table(conn, config.raw_schema, table, csv_path, snapshot_id)

    return row_counts
