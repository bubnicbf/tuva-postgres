"""Exception hierarchy for the tuva_ingest connector.

Every exception here carries a `category` (a short, stable machine-readable
string suitable for storing in `ingestion_runs.error_category`) and produces
a sanitized `str()` -- callers must never interpolate secrets (API tokens,
`PG_DSN`, authorization headers) into these messages. See
`logging_utils.sanitize_error` for the last line of defense used when
logging any exception.
"""
from __future__ import annotations


class ConnectorError(Exception):
    """Base class for all connector errors. `category` is a short, stable
    machine-readable label (e.g. "config", "download", "checksum")."""

    category = "unknown"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigError(ConnectorError):
    category = "config"


class ManifestError(ConnectorError):
    category = "manifest"


class DownloadError(ConnectorError):
    category = "download"


class ChecksumError(ConnectorError):
    category = "checksum"


class ExtractError(ConnectorError):
    category = "extract"


class MigrationError(ConnectorError):
    category = "migration"


class LockError(ConnectorError):
    category = "lock"


class RawLoadError(ConnectorError):
    category = "raw_load"


class StateError(ConnectorError):
    category = "state"


class HealthCheckError(ConnectorError):
    category = "healthcheck"


class CliUsageError(ConnectorError):
    """Invalid CLI input caught by validation *before* any HTTP request or
    SQL statement is issued -- an unknown --endpoint, an invalid --since
    date, or any other unsafe/malformed argument value. Deliberately a
    distinct category from ConfigError (environment-driven configuration)
    so operators/monitoring can tell "bad deployment" apart from "bad
    invocation" at a glance."""

    category = "validation"


class RunNotFoundError(ConnectorError):
    """Raised by `load --run-id` when the given run_id does not resolve to
    a published, successful extraction -- never a silent no-op."""

    category = "run_not_found"


class SecretError(ConnectorError):
    """Raised by `secrets.py` for any failure retrieving/validating the API
    credential from the configured secret provider: an unknown provider, a
    missing secret, malformed secret JSON, a missing `api_token` field, or
    a provider-level (e.g. AWS SDK) error. Never includes the secret value
    itself, or any partial credential content, in its message."""

    category = "secret"


class PaginationError(ConnectorError):
    """Raised by `pagination.py` for any paginated page-request/response
    contract violation: a malformed envelope, missing/invalid metadata, a
    record-count mismatch, a token mismatch, a repeated token (pagination
    cycle), or exceeding the configured maximum page count."""

    category = "pagination"


class ReconciliationError(ConnectorError):
    """Raised when any of the three source/file/database count checks
    (see `paginated_loader.py`) does not match. Always treated as a failed
    run -- the transaction is rolled back and the watermark is never
    committed."""

    category = "reconciliation"


class WatermarkError(ConnectorError):
    """Raised when a candidate high-water mark would move an endpoint's
    durable watermark backward, or when the watermark table cannot be
    read/written safely."""

    category = "watermark"



class ObjectStorageError(ConnectorError):
    """Base class for object-storage backend/publication/verification
    failures (see `object_storage/`). Never includes credentials -- every
    backend authenticates via ambient identity only (see
    `object_storage/s3.py`) and never logs/raises a credential value."""

    category = "object_storage"


class ObjectKeyError(ObjectStorageError):
    """Raised by `object_storage/keys.py` for any unsafe or malformed
    object-key path component (vendor, endpoint, prefix, run_id,
    page_number)."""

    category = "object_key"


class ImmutableObjectError(ObjectStorageError):
    """Raised when a write would overwrite an already-published object
    (a page, the manifest, or the success marker) with *different*
    content. A completed, immutable object is never silently overwritten
    -- see `object_storage/base.StorageBackend.put`."""

    category = "immutable_object"


class RunNotPublishedError(ObjectStorageError):
    """Raised when a run is loaded/replayed without a valid, durable
    success marker and manifest -- see `object_storage/verify.py`. A run
    missing either is never treated as loadable, regardless of how many
    of its pages happen to be present."""

    category = "run_not_published"


class ObjectVerificationError(ObjectStorageError):
    """Raised by `object_storage/verify.py` when a published run fails
    independent re-verification at load time: a missing object, a
    checksum mismatch, a gzip integrity failure, a JSONL record-count
    mismatch, or a manifest reconciliation failure."""

    category = "object_verification"


class RawContractError(ConnectorError):
    """Raised for a failure in the centralized endpoint raw-metadata
    contract (`endpoint_contract.py`) that is not a per-record
    rejection (see `RejectedRecord`/reason codes for those) -- e.g. an
    unsupported endpoint name passed to the contract registry itself."""

    category = "raw_contract"


class CursorError(ConnectorError):
    """Raised when a candidate cursor would move `ops_schema.ingestion_cursor`
    backward for a (vendor/source, endpoint) pair, or when the cursor row
    cannot be locked/validated safely (see `state.py`'s
    `lock_cursor_for_update`/`commit_cursor`). Distinct from the legacy
    `WatermarkError` (`ops_schema.source_watermarks`, used only by the
    local-filesystem paginated workflow in `pagination.py`/
    `paginated_loader.py`) -- the object-storage-backed workflow's sole
    cursor source is `ingestion_cursor` (see docs/SOURCE_CONTRACT.md
    "Cursor safety")."""

    category = "cursor"
