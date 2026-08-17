"""The centralized, authoritative contract for deriving every raw-table
metadata column from one source record (see docs/SOURCE_CONTRACT.md
"Raw metadata definitions" and migrations/006_object_storage_raw_contract.sql
for the physical columns this feeds).

This is the single place `object_raw_loader.py` (and any future caller)
derives:

  `_source_record_id`   a stable, collision-safe identifier for the
                         record within its endpoint, per
                         `ENDPOINT_ID_FIELDS` below. A composite id
                         (medical_claim/pharmacy_claim: claim_id +
                         claim_line_number) is encoded via
                         `encode_composite_id` -- a length-prefixed
                         encoding, never naive delimiter concatenation
                         (so a value that happens to contain the
                         delimiter can never collide with a different
                         logical id).
  `_source_updated_at`  parsed from the endpoint's explicit source
                         update-timestamp field (`updated_at` unless a
                         future verified contract establishes another
                         field for a given endpoint -- see
                         `ENDPOINT_TIMESTAMP_FIELD`). Never substituted
                         with ingestion time when missing/invalid --
                         see `RejectReason.MISSING_SOURCE_TIMESTAMP`/
                         `INVALID_SOURCE_TIMESTAMP`.
  `_payload_hash`        `payload_sha256` below: lowercase SHA-256 over
                         the canonical UTF-8 JSON serialization of the
                         complete record (sorted keys, compact
                         separators) -- identical for the same logical
                         JSON object regardless of input key order.

Every derivation function here returns either a value or a
`RejectReason` (never raises for a data-quality problem) -- classifying
one record as invalid must never abort loading the rest of a page/run.
See `object_raw_loader.classify_record`, the only caller that turns
these into `rejected_record` rows.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .endpoints import table_for_endpoint
from .errors import RawContractError


class RejectReason(str, Enum):
    """Stable, machine-readable reason codes for `rejected_record.reason_code`
    (see migrations/006_object_storage_raw_contract.sql). Never change the
    string value of an existing member -- these are persisted data, not
    just in-process constants."""

    NOT_AN_OBJECT = "not_an_object"
    UNSUPPORTED_ENDPOINT = "unsupported_endpoint"
    MISSING_SOURCE_ID = "missing_source_id"
    MISSING_SOURCE_TIMESTAMP = "missing_source_timestamp"
    INVALID_SOURCE_TIMESTAMP = "invalid_source_timestamp"


class Rejected:
    """Wraps a `RejectReason` plus a short, PHI-free, sanitized detail
    string -- never the raw field value itself (a malformed timestamp's
    *shape*, e.g. "not ISO-8601", is safe to record; the value a patient's
    real update timestamp might accidentally resemble is not worth the
    risk, so this module never interpolates raw field values into detail
    text)."""

    __slots__ = ("reason", "detail")

    def __init__(self, reason: RejectReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail

    def __repr__(self) -> str:  # pragma: no cover - debugging convenience only
        return f"Rejected({self.reason.value}, {self.detail!r})"


# Endpoint contract registry (see module docstring). Keyed by the
# normalized snake_case endpoint name (endpoints.table_for_endpoint's
# output), never the hyphenated CLI --endpoint form.
ENDPOINT_ID_FIELDS: dict[str, tuple[str, ...]] = {
    "eligibility": ("person_id",),
    "medical_claim": ("claim_id", "claim_line_number"),
    "pharmacy_claim": ("claim_id", "claim_line_number"),
}

# `updated_at` unless a future verified per-endpoint contract establishes
# a different field (see module docstring) -- one shared constant here,
# not a per-endpoint override table, precisely because no endpoint has
# ever needed one yet; add a per-endpoint entry only once a real,
# verified source contract requires it.
DEFAULT_TIMESTAMP_FIELD = "updated_at"
ENDPOINT_TIMESTAMP_FIELD: dict[str, str] = {
    "eligibility": DEFAULT_TIMESTAMP_FIELD,
    "medical_claim": DEFAULT_TIMESTAMP_FIELD,
    "pharmacy_claim": DEFAULT_TIMESTAMP_FIELD,
}


def normalized_endpoint(endpoint: str) -> str:
    """Accept either the hyphenated CLI endpoint form or an
    already-normalized snake_case table name and return the normalized
    snake_case form -- `table_for_endpoint` already raises
    `CliUsageError` for anything unrecognized in either form (since
    `ENDPOINT_TABLE_MAP`'s values -- the snake_case table names -- are
    never themselves keys of that map, calling this with an
    already-normalized name would raise; callers therefore always pass
    the raw --endpoint value they received, never a pre-normalized one)."""
    return table_for_endpoint(endpoint)


def canonical_json_bytes(value: Any) -> bytes:
    """The documented canonical UTF-8 JSON serialization used by
    `payload_sha256`: sorted object keys at every nesting level, compact
    separators (no extra whitespace), non-ASCII characters kept as
    literal UTF-8 (never \\uXXXX-escaped) so two different key orderings
    of the same logical JSON object always serialize to byte-identical
    output."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def payload_sha256(value: Any) -> str:
    """Lowercase hex SHA-256 over `canonical_json_bytes(value)` -- the
    same logical JSON object hashes identically regardless of input key
    order (see module docstring; tested explicitly in
    tests/unit/test_endpoint_contract.py)."""
    import hashlib

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def encode_composite_id(parts: tuple[str, ...]) -> str:
    """Length-prefixed encoding of an ordered tuple of id-part strings:
    `"<len1>:<part1><len2>:<part2>..."`, each length measured in UTF-8
    bytes. Unambiguous and collision-safe -- unlike naive delimiter
    concatenation (`f"{a}-{b}"`), two different `(a, b)` pairs can never
    encode to the same string, because each part's exact byte length is
    recorded immediately before it, so a delimiter-like substring
    *inside* a part can never be misread as a real separator."""
    encoded_parts = []
    for part in parts:
        part_bytes = part.encode("utf-8")
        encoded_parts.append(f"{len(part_bytes)}:{part}")
    return "".join(encoded_parts)


def _stringify_id_component(value: Any) -> str | None:
    """Coerce one id-field's raw JSON value to its canonical string form.
    Accepts str/int/float (never bool, never a nested object/array --
    those are not valid identifier shapes and are rejected as missing).
    A float is only accepted when it is integral (JSON numbers with no
    fractional part are often decoded as float by some JSON producers);
    a fractional float is rejected -- a claim_line_number/person_id is
    never fractional."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return None
    return None


def derive_source_record_id(endpoint: str, record: dict[str, Any]) -> str | Rejected:
    """Derive `_source_record_id` for one record of `endpoint` (already
    normalized snake_case -- see `normalized_endpoint`). Returns
    `Rejected(RejectReason.MISSING_SOURCE_ID, ...)` if any required id
    field is missing, blank, or not a safe scalar shape."""
    id_fields = ENDPOINT_ID_FIELDS.get(endpoint)
    if id_fields is None:
        raise RawContractError(f"no source-record-id contract registered for endpoint {endpoint!r}")

    parts: list[str] = []
    for field in id_fields:
        raw_value = record.get(field)
        part = _stringify_id_component(raw_value)
        if part is None:
            return Rejected(
                RejectReason.MISSING_SOURCE_ID,
                f"endpoint {endpoint!r} record is missing a valid '{field}' value",
            )
        parts.append(part)

    if len(parts) == 1:
        return parts[0]
    return encode_composite_id(tuple(parts))


def derive_source_updated_at(endpoint: str, record: dict[str, Any]) -> datetime | Rejected:
    """Derive `_source_updated_at` for one record of `endpoint` from its
    explicit source update-timestamp field (see `ENDPOINT_TIMESTAMP_FIELD`).
    Never falls back to ingestion time -- a missing or unparseable value
    is always a rejection, never a substitution (see module docstring)."""
    field = ENDPOINT_TIMESTAMP_FIELD.get(endpoint, DEFAULT_TIMESTAMP_FIELD)
    raw_value = record.get(field)

    if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
        return Rejected(
            RejectReason.MISSING_SOURCE_TIMESTAMP,
            f"endpoint {endpoint!r} record is missing a value for source timestamp field '{field}'",
        )
    if not isinstance(raw_value, str):
        return Rejected(
            RejectReason.INVALID_SOURCE_TIMESTAMP,
            f"endpoint {endpoint!r} record field '{field}' is not a string timestamp "
            f"(got {type(raw_value).__name__})",
        )

    normalized = raw_value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return Rejected(
            RejectReason.INVALID_SOURCE_TIMESTAMP,
            f"endpoint {endpoint!r} record field '{field}' is not a valid ISO-8601 timestamp",
        )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
