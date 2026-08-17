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
