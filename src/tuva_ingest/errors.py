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
