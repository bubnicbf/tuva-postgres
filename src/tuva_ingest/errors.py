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


class OAuthError(ConnectorError):
    """Raised by `oauth.py` for any OAuth token-lifecycle failure: a
    malformed or incomplete token-endpoint response, an unsupported
    token_type, a permanent grant failure (invalid_client/invalid_grant/
    ...), or a transient failure that exhausted the shared bounded retry
    budget. Never includes a token, client secret, or the raw
    token-endpoint response body in its message."""

    category = "oauth"


class QuarantineError(ConnectorError):
    """Raised when a structurally invalid record cannot be safely written
    to the quarantine table (e.g. a database error while inserting a
    quarantine row) -- always treated as a failed run, rolling back the
    whole load transaction so a partially-quarantined run is never left
    committed."""

    category = "quarantine"


class RawContractError(ConnectorError):
    """Raised by `endpoint_contract.py` for a violation of the per-endpoint
    raw-record identity contract that is a caller/configuration bug, not a
    per-record data-quality problem (compare `Rejected`, which represents an
    individual record's own defect and never raises): today, only "no
    source-record-id contract registered for this endpoint" -- reaching
    `object_raw_loader.py`/`object_extract.py` with an endpoint that has no
    registered `derive_source_record_id`/`derive_source_updated_at` mapping
    means an endpoint was wired into the object-storage-backed workflow
    without its identity contract ever being defined."""

    category = "raw_contract"


class ObjectKeyError(ConnectorError):
    """Raised by `object_storage/keys.py` for a malformed object-storage key
    component: an unsafe/invalid storage prefix, a `run_id` that is not a
    valid UUID, a `load_date` that was not derived from a timezone-aware
    datetime, or a `page_number` outside the valid 1-999999 range. Always a
    caller/programming-contract violation caught before any object-storage
    I/O is attempted."""

    category = "object_key"


class ObjectStorageError(ConnectorError):
    """Raised by an `object_storage/` backend (`local.py`/`s3.py`) for an
    I/O-level failure: an unsafe relative path outside the configured root,
    or an underlying S3 API failure (`put_object`/`get_object`/
    `head_object`). Never includes credentials in its message."""

    category = "object_storage"


class ImmutableObjectError(ConnectorError):
    """Raised by `object_storage/publish.py` when a conditional
    (create-if-absent) write to an immutable object-storage key would
    overwrite existing content -- a page, manifest, or success-marker key is
    only ever written once; a second, conflicting write attempt for the
    same key is a bug (a reused `run_id`/page number, or a real concurrent
    publisher) and must fail loudly rather than silently overwrite durable,
    already-published data."""

    category = "immutable_object"


class ObjectVerificationError(ConnectorError):
    """Raised by `object_storage/verify.py` when a published run's success
    marker, manifest, or a page object fails re-verification at load time:
    malformed JSON, a checksum/size mismatch, or any other structural
    inconsistency between what was published and what is now being read
    back. Always treated as a failed load -- the load transaction is never
    started (or is rolled back) and `state.mark_run_failed` is called
    separately (see `object_raw_loader.py`, `cli._run_object_load`)."""

    category = "object_verification"


class RunNotPublishedError(ConnectorError):
    """Raised by `object_storage/verify.py` when a run's success marker (or
    manifest) does not exist in object storage at all -- distinct from
    `ObjectVerificationError` (marker/manifest exists but fails
    verification) and from `RunNotFoundError` (no `ingestion_run` database
    row for the given run_id). A `load --run-id X` for a run whose
    extraction never reached "published" (crashed mid-extraction, or the
    wrong run_id) raises this before any database transaction is opened."""

    category = "object_verification"


class CursorError(ConnectorError):
    """Raised by `state.py`'s canonical object-storage-backed cursor
    functions for either of two distinct cursor-safety violations, both
    always fatal to the current load (see `object_raw_loader.py`,
    `cli._run_object_load`): (1) a candidate cursor that would move
    `ops.ingestion_cursor.committed_cursor` backward for a (vendor,
    endpoint) pair (checked by the caller after `state.lock_cursor_for_update`
    returns the currently committed value); (2) an optimistic-concurrency
    `lock_version` mismatch on `state.commit_cursor`'s UPDATE -- which,
    since the row is held with `SELECT ... FOR UPDATE` for the whole
    transaction, can only happen because of a caller bug, never a
    legitimate concurrent race. Never includes cursor values from an
    untrusted source verbatim beyond what the source itself already
    returned as the high-water mark."""

    category = "cursor"


class OperationalStateError(ConnectorError):
    """Raised by `state.py`'s canonical object-storage-backed lifecycle
    functions when a write that is supposed to represent forward progress
    through `ops.ingestion_run.status`'s state machine (running ->
    published -> loading -> committed) or a page's immutable identity
    affects zero rows, or affects a row whose immutable fields disagree
    with what the caller is asserting -- distinct from `mark_run_failed`,
    for which a zero-row update is a deliberate, documented, safe no-op
    (see that function's own docstring). Two distinct situations raise
    this:

    (1) `mark_run_published`/`mark_run_load_started`/`mark_run_committed`
        matched zero rows -- the run was not in the exact prior status the
        transition requires (e.g. `mark_run_committed` requires the run to
        currently be `loading`). This is never silently treated as
        success: an operator/caller must not be told a run committed when
        its own `ingestion_run` row never actually made that transition
        (see "Do not allow a committed run to be reset or overwritten
        accidentally" in docs/RUNBOOK.md).

    (2) `insert_ingestion_page` was called for a `(run_id, page_number)`
        that already has a row on file, but with a different `object_key`,
        `checksum`, or `source_record_count` than the row already
        recorded -- an idempotent retry may only update mutable
        verification/load-result columns (`accepted_count`,
        `rejected_count`, `verified_at`, `status`); it must never silently
        replace an already-recorded page's immutable identity with
        conflicting values. This always indicates either a caller bug (a
        `run_id`/page number reused for genuinely different content) or
        upstream data corruption, and must fail loudly rather than
        accept whichever value happened to arrive most recently."""

    category = "operational_state"
