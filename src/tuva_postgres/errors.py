"""Exception hierarchy for the tuva_postgres ingestion pipeline.

Every exception here carries a `category` (a short, stable machine-readable
string suitable for storing in `pipeline_runs.error_category`) and produces
a sanitized `str()` -- callers must never interpolate secrets (API tokens,
`PG_DSN`, authorization headers) into these messages. See
`logging_utils.sanitize_error` for the last line of defense used when
logging any exception.
"""
from __future__ import annotations


class PipelineError(Exception):
    """Base class for all pipeline errors. `category` is a short, stable
    machine-readable label (e.g. "config", "download", "checksum")."""

    category = "unknown"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ConfigError(PipelineError):
    category = "config"


class ManifestError(PipelineError):
    category = "manifest"


class DownloadError(PipelineError):
    category = "download"


class ChecksumError(PipelineError):
    category = "checksum"


class LandingError(PipelineError):
    category = "landing"


class MigrationError(PipelineError):
    category = "migration"


class LockError(PipelineError):
    category = "lock"


class LoadError(PipelineError):
    category = "load"


class TestError(PipelineError):
    category = "tests"


class HealthCheckError(PipelineError):
    category = "healthcheck"
