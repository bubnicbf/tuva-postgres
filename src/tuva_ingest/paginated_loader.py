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
`_source_row_number` assigned as a running counter over only the
*structurally valid* records (continuing across page boundaries) -- see
`load_paginated_run`. This reuses the exact same two columns the legacy
CSV contract already populates (`raw_loader.py`'s `_RAW_COLUMNS`), so
both contracts' rows coexist in the same physical tables without any
schema fork.

Quarantine
-----------
Every record is classified by `validators.validate_record` before it is
staged: a structurally valid record is loaded into the raw table exactly
as before; a structurally invalid one is instead written to the
restricted `quarantined_records` table (see `quarantine.py`,
`migrations/006_record_quarantine.sql`) and is *never* also loaded into
the raw table. `load_paginated_run` returns a `LoadCounts` (valid vs.
quarantined per page and in total) that the caller (`cli.py`) uses for
the three-way reconciliation this connector requires:
`source_record_count == raw_loaded_count + quarantined_count`.

`quarantined_count` is computed by re-classifying every record on every
call to `load_paginated_run` (the same deterministic pass over the
immutable page files this function already performs), never by
`SELECT`-ing the quarantine table back -- `ingest_role` is deliberately
granted only `INSERT` on `quarantined_records` (see migrations/006's
access-model comment), so this function must never need `SELECT` on it
to compute a correct count, including on an idempotent repeat load.
"""
from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .db import qualified_relation
from .endpoints import table_for_endpoint
from .errors import RawLoadError, ReconciliationError
from .pagination import PaginatedRunStore, file_sha256
from .logging_utils import log_event
from .quarantine import insert_quarantine_record
from .validators import validate_record

_STAGING_TABLE = "_tuva_ingest_page_staging"


@dataclass(frozen=True)
class LoadCounts:
    valid_count: int
    quarantined_count: int
    quarantined_by_reason: dict


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


def load_paginated_run(conn, config, store: PaginatedRunStore, run_id: str, manifest: dict, *, logger: object = None) -> LoadCounts:
    """Load every page of `manifest` into `config.raw_schema`'s table for
    `manifest['endpoint']`, inside the caller's existing transaction --
    never commits or rolls back itself, and never touches any raw table
    other than the one endpoint's. Every record is classified by
    `validators.validate_record` first: a structurally valid record is
    staged into the raw table exactly as before; a structurally invalid
    one is instead inserted into the restricted `quarantined_records`
    table (see `quarantine.py`) and is never also loaded into the raw
    table. Idempotent: repeating this for the same `run_id` inserts
    nothing new into either table (`ON CONFLICT DO NOTHING` on both,
    backed by migrations/005's and migrations/006's unique indexes)
    rather than duplicating rows.

    Returns a `LoadCounts` computed from this call's own classification
    pass (valid vs. quarantined, and quarantined-by-reason-code) -- see
    module docstring for why this is never a `SELECT` against the
    quarantine table. Raises `RawLoadError` if a page's valid records
    cannot be staged/copied, or `QuarantineError` if a quarantine insert
    fails.
    """
    from .errors import QuarantineError

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
    valid_count = 0
    quarantined_count = 0
    quarantined_by_reason: dict[str, int] = {}

    for page in manifest["pages"]:
        page_path = run_dir / page["file_name"]
        page_number = page["page_number"]
        source_page_token = page.get("response_page_token") or page.get("request_page_token")
        retrieved_at = page["retrieved_at"]
        file_sha256 = page["sha256"]

        # Classify every record first, buffering quarantine candidates in
        # memory for this one page (a page is bounded by TUVA_API_MAX_PAGE_BYTES,
        # so this is never unbounded) -- quarantine rows are inserted via a
        # separate statement *after* the COPY below completes, since
        # PostgreSQL's wire protocol does not allow another statement to
        # be issued on the same connection while a COPY FROM STDIN is
        # still open.
        quarantine_candidates: list[tuple[int, object, object]] = []  # (record_index, decision, record)

        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {_STAGING_TABLE}")
            try:
                with cur.copy(
                    f"COPY {_STAGING_TABLE} (_snapshot_id, _source_row_number, _loaded_at, raw_row, "
                    "endpoint, page_number, source_page_token, retrieved_at, file_sha256) FROM STDIN"
                ) as copy:
                    for record_index, record in enumerate(_iter_jsonl_records(page_path), start=1):
                        decision = validate_record(endpoint, record)
                        if decision is None:
                            offset += 1
                            valid_count += 1
                            copy.write_row((
                                run_id, offset, loaded_at, json.dumps(record, default=str),
                                endpoint, page_number, source_page_token, retrieved_at, file_sha256,
                            ))
                        else:
                            quarantined_count += 1
                            quarantined_by_reason[decision.reason_code] = (
                                quarantined_by_reason.get(decision.reason_code, 0) + 1
                            )
                            quarantine_candidates.append((record_index, decision, record))
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

        for record_index, decision, record in quarantine_candidates:
            try:
                fingerprint = insert_quarantine_record(
                    conn, config.ops_schema, run_id=run_id, source=config.source_name,
                    endpoint=endpoint, page_number=page_number, record_index=record_index,
                    decision=decision, record=record,
                )
            except Exception as exc:
                raise QuarantineError(
                    f"{endpoint!r}: failed to quarantine record {record_index} of page "
                    f"{page['file_name']!r} (reason {decision.reason_code!r}): {exc}"
                ) from exc
            if logger is not None:
                log_event(
                    logger, "record_quarantined", run_id=run_id, endpoint=endpoint,
                    page_number=page_number, record_index=record_index,
                    reason_code=decision.reason_code, source_record_sha256=fingerprint,
                )

    if logger is not None:
        log_event(
            logger, "page_reconciled", run_id=run_id, endpoint=endpoint,
            valid_count=valid_count, quarantined_count=quarantined_count,
        )

    return LoadCounts(
        valid_count=valid_count, quarantined_count=quarantined_count, quarantined_by_reason=quarantined_by_reason
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
