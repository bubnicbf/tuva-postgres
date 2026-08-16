"""Database access for the restricted PHI-bearing quarantine table (see
`migrations/006_record_quarantine.sql`). Kept separate from
`validators.py` (pure, DB-free structural classification) and
`paginated_loader.py` (orchestrates classify -> route-to-raw-or-quarantine
inside the load transaction) so each concern -- "is this record valid",
"how do I store an invalid one", "how do I load a whole run" -- has
exactly one home.

Every function here uses validated, quoted identifiers
(`db.qualified_relation`) for the one dynamic piece of SQL *syntax*
(`ops_schema`) and ordinary parameterized `%s` values for everything
else -- run_id, endpoint, page number, record index, reason code/detail,
the record itself (bound as a JSONB value, never interpolated into SQL
text), and the record's own SHA-256 fingerprint. Never selects this
table back (see migrations/006's access-model comment -- `ingest_role`
is INSERT-only by design); reconciliation counts the rows this
process itself just inserted via each `INSERT`'s own affected-row count
within the same transaction, never a separate `SELECT`.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from .db import qualified_relation
from .validators import QuarantineDecision, decision_detail

_QUARANTINE_TABLE = "quarantined_records"


def _relation(ops_schema: str) -> str:
    return qualified_relation(ops_schema, _QUARANTINE_TABLE, schema_label="ops_schema", relation_label="table")


def record_fingerprint(record: object) -> str:
    """A non-reversible SHA-256 fingerprint of the exact record content
    (deterministic, sorted-key JSON serialization) -- stored alongside
    the record itself (`source_record_sha256`) so an operator/audit
    process can correlate a quarantined row with the corresponding line
    in the immutable page file it came from without needing to compare
    full JSON payloads."""
    encoded = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def insert_quarantine_record(
    conn,
    ops_schema: str,
    *,
    run_id: str,
    source: str,
    endpoint: str,
    page_number: int,
    record_index: int,
    decision: QuarantineDecision,
    record: object,
) -> str:
    """Insert one quarantine row inside the caller's existing transaction
    -- never commits or rolls back itself. Idempotent: repeating this for
    the same `(run_id, page_number, record_index)` is a safe no-op
    (`ON CONFLICT DO NOTHING`, backed by
    migrations/006's unique index), matching the same idempotency shape
    the raw tables use for a repeated `load --run-id <same value>`.
    `record` is bound as an ordinary JSONB parameter -- never
    interpolated into SQL text -- so it can be anything the source sent,
    including a record that itself failed `record_not_object` (a
    non-dict value); `json.dumps(..., default=str)` handles any
    JSON-incompatible leftovers defensively.

    Returns the record's fingerprint (see `record_fingerprint`) so the
    caller can emit a `record_quarantined` structured log event
    containing only safe metadata -- run id, endpoint, page/record
    position, reason code, and this fingerprint -- never the raw record
    itself."""
    relation = _relation(ops_schema)
    fingerprint = record_fingerprint(record)
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {relation} "
            "(run_id, source, endpoint, page_number, record_index, reason_code, reason_detail, "
            "raw_record, source_record_sha256, quarantined_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (run_id, page_number, record_index) DO NOTHING",
            (
                run_id,
                source,
                endpoint,
                page_number,
                record_index,
                decision.reason_code,
                decision_detail(decision),
                json.dumps(record, default=str),
                fingerprint,
                datetime.now(timezone.utc),
            ),
        )
    return fingerprint
