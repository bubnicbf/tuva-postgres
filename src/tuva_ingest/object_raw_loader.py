"""Load a verified, object-storage-published run into its one endpoint's
raw table -- idempotently, inside the caller's single atomic transaction
(see docs/SOURCE_CONTRACT.md "COPY and transactional merge" and
cli._run_object_load, the only caller that owns this transaction's
commit/rollback boundary).

Pattern (mirrors the exact 15-step contract documented in
docs/SOURCE_CONTRACT.md, and structurally parallels
`paginated_loader.load_paginated_run`'s existing per-page
TRUNCATE-temp -> COPY -> merge loop, extended with the new raw-metadata
contract, rejection handling, schema observations, and cursor safety):

  1. `object_storage.verify.load_and_verify_manifest` re-verifies the
     run's success marker, manifest, and every page's checksum/gzip/
     record-count BEFORE this module touches the database at all.
  2. Records are parsed and classified one page at a time
     (`object_storage.verify.iter_verified_page_records`) -- a run is
     never loaded fully into memory.
  3. Each record is classified via `endpoint_contract.derive_source_record_id`/
     `derive_source_updated_at` -- accepted rows get every raw-metadata
     column; rejected rows become `rejected_record` rows instead (never
     raise -- one bad record must never abort the rest of a page/run).
  4-6. Accepted rows for each page are `COPY`'d into a transaction-local
     TEMP TABLE, then merged into the permanent raw table via
     `INSERT ... SELECT ... ON CONFLICT (...) DO NOTHING` against the
     source-stable partial unique index (migrations/006).
  7-9. Per-page and run-level counts are accumulated and reconciled:
     `source records == accepted + rejected` and
     `accepted == inserted + exact duplicates` (see `_reconcile`).
  10-11. The (vendor, endpoint) cursor row is locked
     (`state.lock_cursor_for_update`) and validated for backward
     movement (`errors.CursorError`) -- only updated
     (`state.commit_cursor`) after every check above has already
     succeeded.
  12. This function never commits -- the caller's one `conn.commit()`
     (after this function returns successfully) is what makes the raw
     merge, every operational write, and the cursor advance all become
     visible together.
  13-14. Any exception here leaves the transaction in a state the caller
     must roll back (`conn.rollback()`); the caller then calls
     `state.mark_run_failed` in a SEPARATE, freshly-committed write (see
     that function's own docstring) -- never from inside this function.
  15. No function in this module or in `state.py`'s "canonical
     object-storage-backed operational model" section ever calls
     `conn.commit()`/`conn.rollback()` itself.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from . import endpoint_contract, schema_observation, state
from .db import qualified_relation
from .endpoint_contract import Rejected, RejectReason
from .errors import CursorError, RawLoadError, ReconciliationError
from .logging_utils import log_event
from .object_storage.base import StorageBackend
from .object_storage.keys import RunKey
from .object_storage.verify import VerifiedRun, iter_verified_page_records, load_and_verify_manifest

_STAGING_TABLE = "_tuva_object_raw_staging"
_STAGING_COLUMNS = (
    "_ingestion_run_id", "_ingested_at", "_source_endpoint", "_source_record_id",
    "_source_updated_at", "_payload_hash", "_raw_payload",
)
_STAGING_COLUMNS_SQL = ", ".join(_STAGING_COLUMNS)


def _relation(raw_schema: str, table: str) -> str:
    return qualified_relation(raw_schema, table, schema_label="raw_schema", relation_label="table")


@dataclass
class PageLoadCounts:
    page_number: int
    source_record_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    inserted_count: int = 0
    duplicate_count: int = 0


@dataclass
class LoadResult:
    run_id: str
    endpoint: str
    table: str
    candidate_cursor: str
    extracted_count: int
    accepted_count: int
    rejected_count: int
    inserted_count: int
    duplicate_count: int
    page_counts: list[PageLoadCounts] = field(default_factory=list)


@dataclass(frozen=True)
class ClassifiedRecord:
    accepted: dict[str, Any] | None
    rejected: Rejected | None
    # The source_record_id, when it was safely derivable, regardless of
    # whether the record was ultimately accepted or rejected for a LATER
    # reason (e.g. a missing/invalid source timestamp) -- included in
    # rejected_record rows "when safely available" per
    # docs/SOURCE_CONTRACT.md "Rejected records".
    source_record_id: str | None


def _classify_record(endpoint: str, record: Any) -> ClassifiedRecord:
    """Never raises for a data-quality problem -- see module docstring."""
    if not isinstance(record, dict):
        return ClassifiedRecord(None, Rejected(RejectReason.NOT_AN_OBJECT, "record is not a JSON object"), None)

    source_record_id = endpoint_contract.derive_source_record_id(endpoint, record)
    if isinstance(source_record_id, Rejected):
        return ClassifiedRecord(None, source_record_id, None)

    source_updated_at = endpoint_contract.derive_source_updated_at(endpoint, record)
    if isinstance(source_updated_at, Rejected):
        return ClassifiedRecord(None, source_updated_at, source_record_id)

    payload_hash = endpoint_contract.payload_sha256(record)
    return ClassifiedRecord(
        {
            "_source_endpoint": endpoint,
            "_source_record_id": source_record_id,
            "_source_updated_at": source_updated_at,
            "_payload_hash": payload_hash,
            "_raw_payload": record,
        },
        None,
        source_record_id,
    )


def _reconcile(counts: PageLoadCounts) -> None:
    if counts.accepted_count + counts.rejected_count != counts.source_record_count:
        raise ReconciliationError(
            f"page {counts.page_number}: accepted ({counts.accepted_count}) + rejected "
            f"({counts.rejected_count}) does not equal source record count ({counts.source_record_count})"
        )
    if counts.inserted_count + counts.duplicate_count != counts.accepted_count:
        raise ReconciliationError(
            f"page {counts.page_number}: inserted ({counts.inserted_count}) + duplicate "
            f"({counts.duplicate_count}) does not equal accepted count ({counts.accepted_count})"
        )


def load_verified_run(
    conn,
    config,
    backend: StorageBackend,
    run_key: RunKey,
    *,
    logger: Any = None,
) -> LoadResult:
    """Verify, then load, one run -- see module docstring. Raises
    `ObjectVerificationError`/`RunNotPublishedError` (from
    `object_storage.verify`), `ReconciliationError`, or `CursorError` on
    any failure; the caller is responsible for `conn.rollback()` and a
    separate `state.mark_run_failed` call in every case (see
    cli._run_object_load)."""
    verified: VerifiedRun = load_and_verify_manifest(backend, run_key)
    manifest = verified.manifest
    run_id = manifest["run_id"]
    vendor = manifest["vendor"]
    endpoint = manifest["endpoint"]  # already the normalized snake_case table name (== the raw table name)
    table = endpoint
    candidate_cursor = manifest["candidate_cursor"]
    ops_schema = config.ops_schema
    raw_schema = config.raw_schema
    relation = _relation(raw_schema, table)
    staging_relation = f'"{_STAGING_TABLE}"'

    with conn.cursor() as cur:
        cur.execute(
            f"CREATE TEMP TABLE IF NOT EXISTS {staging_relation} "
            f"({_STAGING_COLUMNS[0]} uuid, {_STAGING_COLUMNS[1]} timestamptz, {_STAGING_COLUMNS[2]} text, "
            f"{_STAGING_COLUMNS[3]} text, {_STAGING_COLUMNS[4]} timestamptz, {_STAGING_COLUMNS[5]} text, "
            f"{_STAGING_COLUMNS[6]} jsonb) ON COMMIT DROP"
        )

    run_extracted = 0
    run_accepted = 0
    run_rejected = 0
    run_inserted = 0
    run_duplicate = 0
    page_counts: list[PageLoadCounts] = []

    for page in manifest["pages"]:
        page_number = page["page_number"]
        counts = PageLoadCounts(page_number=page_number)
        rejected_rows: list[dict[str, Any]] = []
        observations: dict[str, set[str]] = {}
        ingested_at = datetime.now(timezone.utc)

        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE TABLE {staging_relation}")
            try:
                with cur.copy(f"COPY {staging_relation} ({_STAGING_COLUMNS_SQL}) FROM STDIN") as copy:
                    for position, record in enumerate(iter_verified_page_records(backend, page), start=1):
                        counts.source_record_count += 1
                        try:
                            page_observations = schema_observation.walk_paths(record) if isinstance(record, dict) else {}
                            observations = schema_observation.merge_paths(observations, page_observations)
                        except Exception:  # pragma: no cover - defensive; observation must never break loading
                            pass

                        classified = _classify_record(endpoint, record)
                        if classified.rejected is not None:
                            counts.rejected_count += 1
                            rejected_rows.append(
                                {
                                    "run_id": run_id,
                                    "page_number": page_number,
                                    "record_position": position,
                                    "reason_code": classified.rejected.reason.value,
                                    "detail": classified.rejected.detail,
                                    "source_record_id": classified.source_record_id,
                                    "payload_hash": (
                                        endpoint_contract.payload_sha256(record) if isinstance(record, dict) else None
                                    ),
                                    "raw_object_key": page["object_key"],
                                }
                            )
                            continue

                        accepted = classified.accepted
                        counts.accepted_count += 1
                        copy.write_row((
                            run_id, ingested_at, accepted["_source_endpoint"], accepted["_source_record_id"],
                            accepted["_source_updated_at"], accepted["_payload_hash"],
                            json.dumps(accepted["_raw_payload"], default=str),
                        ))
            except Exception as exc:
                raise RawLoadError(f"{table!r}: failed while staging page {page_number}: {exc}") from exc

            cur.execute(
                f"INSERT INTO {relation} ({_STAGING_COLUMNS_SQL}) "
                f"SELECT {_STAGING_COLUMNS_SQL} FROM {staging_relation} "
                f"ON CONFLICT (_source_endpoint, _source_record_id, _source_updated_at, _payload_hash) "
                f"WHERE _source_record_id IS NOT NULL DO NOTHING"
            )
            counts.inserted_count = cur.rowcount
            counts.duplicate_count = counts.accepted_count - counts.inserted_count

        _reconcile(counts)

        if rejected_rows:
            state.insert_rejected_records(conn, ops_schema, rejected_rows)

        if observations:
            page_fingerprint = schema_observation.fingerprint(observations)
            state.upsert_schema_observations(
                conn, ops_schema, vendor=vendor, endpoint=endpoint, run_id=run_id, page_number=page_number,
                observations=observations, fingerprint=page_fingerprint,
            )

        state.insert_ingestion_page(
            conn, ops_schema, run_id=run_id, page_number=page_number, object_key=page["object_key"],
            checksum=page["sha256"], compressed_size_bytes=page["compressed_size_bytes"],
            source_record_count=counts.source_record_count, accepted_count=counts.accepted_count,
            rejected_count=counts.rejected_count, request_cursor=page.get("request_cursor"),
            response_cursor=page.get("response_cursor"), next_page_cursor=page.get("next_page_cursor"),
            retrieved_at=page.get("retrieved_at"), status="loaded",
        )

        if logger is not None:
            log_event(
                logger, "object_page_loaded", run_id=run_id, endpoint=endpoint, page_number=page_number,
                accepted_count=counts.accepted_count, rejected_count=counts.rejected_count,
                inserted_count=counts.inserted_count, duplicate_count=counts.duplicate_count,
            )

        run_extracted += counts.source_record_count
        run_accepted += counts.accepted_count
        run_rejected += counts.rejected_count
        run_inserted += counts.inserted_count
        run_duplicate += counts.duplicate_count
        page_counts.append(counts)

    if run_extracted != manifest["total_record_count"]:
        raise ReconciliationError(
            f"run {run_id!r}: total records processed ({run_extracted}) does not equal the manifest's "
            f"total_record_count ({manifest['total_record_count']})"
        )

    # --- cursor safety: lock, validate, and only then advance ------------
    locked = state.lock_cursor_for_update(conn, ops_schema, vendor, endpoint)
    committed_cursor, _prior_run_id, lock_version = locked
    if committed_cursor is not None and candidate_cursor is not None and candidate_cursor < committed_cursor:
        raise CursorError(
            f"candidate cursor {candidate_cursor!r} for (vendor={vendor!r}, endpoint={endpoint!r}) would "
            f"move the committed cursor backward from {committed_cursor!r} -- refusing to commit (see "
            "docs/SOURCE_CONTRACT.md 'Cursor safety')"
        )

    state.commit_cursor(
        conn, ops_schema, vendor, endpoint, committed_cursor=candidate_cursor, successful_run_id=run_id,
        expected_lock_version=lock_version,
    )

    state.mark_run_committed(
        conn, ops_schema, run_id, accepted_count=run_accepted, rejected_count=run_rejected,
        inserted_count=run_inserted, duplicate_count=run_duplicate,
    )

    return LoadResult(
        run_id=run_id, endpoint=endpoint, table=table, candidate_cursor=candidate_cursor,
        extracted_count=run_extracted, accepted_count=run_accepted, rejected_count=run_rejected,
        inserted_count=run_inserted, duplicate_count=run_duplicate, page_counts=page_counts,
    )
