"""Environment-driven configuration for the ingestion pipeline.

Every deployment setting is an environment variable (see
scripts/setup_env.example for the full list with non-secret examples).
`PipelineConfig.load()` fails fast with a single, clear `ConfigError`
listing every problem found, rather than raising on the first one -- an
operator fixing a broken `.env` should see everything wrong at once.

Secrets (`api_token`, `pg_dsn`) are never included in `repr()`/`str()` or
in any log line. Use `PipelineConfig.safe_dict()` for anything that might
be logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .identifiers import InvalidIdentifierError, validate_identifier

# The full set of fields a given CLI command needs populated. Commands that
# don't touch the API (e.g. `migrate`, `healthcheck`) shouldn't fail
# because TUVA_API_TOKEN is unset.
REQUIRE_API = frozenset({"api_manifest_url", "api_token"})
REQUIRE_DB = frozenset({"pg_dsn"})
REQUIRE_RAW_DATA = frozenset({"raw_data_dir"})

ALL_REQUIREMENTS = REQUIRE_API | REQUIRE_DB | REQUIRE_RAW_DATA


def _validate_identifier(name: str, value: str, errors: list[str]) -> None:
    """Validate `value` against the shared identifier policy (see
    identifiers.py), appending a message to `errors` instead of raising --
    `PipelineConfig.load()` collects every configuration problem at once
    rather than stopping at the first one, so a single bad env var doesn't
    hide every other mistake in a broken `.env`."""
    try:
        validate_identifier(value, name)
    except InvalidIdentifierError as exc:
        errors.append(str(exc))


def _env_float(name: str, default: float, errors: list[str]) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a valid number")
        return default


def _env_int(name: str, default: int, errors: list[str]) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        errors.append(f"{name}={raw!r} is not a valid integer")
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PipelineConfig:
    api_manifest_url: str | None
    api_token: str | None
    api_timeout_seconds: float
    api_max_retries: int
    api_allow_insecure_http: bool

    raw_data_dir: Path

    pg_dsn: str | None
    pg_schema: str
    terminology_schema: str
    ops_schema: str

    pipeline_environment: str
    pipeline_max_success_age_hours: float

    log_level: str
    metrics_file: Path | None

    source_name: str = field(default="tuva")

    @classmethod
    def load(cls, required: frozenset[str] = ALL_REQUIREMENTS) -> "PipelineConfig":
        errors: list[str] = []

        api_manifest_url = os.environ.get("TUVA_API_MANIFEST_URL") or None
        api_token = os.environ.get("TUVA_API_TOKEN") or None
        api_timeout_seconds = _env_float("TUVA_API_TIMEOUT_SECONDS", 30.0, errors)
        api_max_retries = _env_int("TUVA_API_MAX_RETRIES", 5, errors)
        api_allow_insecure_http = _env_bool("TUVA_API_ALLOW_INSECURE_HTTP", False)

        raw_data_dir_raw = os.environ.get("RAW_DATA_DIR", "data/raw")
        raw_data_dir = Path(raw_data_dir_raw)

        pg_dsn = os.environ.get("PG_DSN") or None
        pg_schema = os.environ.get("PG_SCHEMA", "tuva")
        terminology_schema = os.environ.get("TERMINOLOGY_SCHEMA", f"{pg_schema}_term")
        ops_schema = os.environ.get("OPS_SCHEMA", "tuva_ops")

        pipeline_environment = os.environ.get("PIPELINE_ENVIRONMENT", "local")
        pipeline_max_success_age_hours = _env_float("PIPELINE_MAX_SUCCESS_AGE_HOURS", 30.0, errors)

        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
        metrics_file_raw = os.environ.get("METRICS_FILE") or None
        metrics_file = Path(metrics_file_raw) if metrics_file_raw else None

        # --- required-field checks, scoped to what this command needs -----
        if "api_manifest_url" in required and not api_manifest_url:
            errors.append("TUVA_API_MANIFEST_URL is required but not set")
        if api_manifest_url and not api_manifest_url.startswith(("https://", "http://")):
            errors.append(f"TUVA_API_MANIFEST_URL={api_manifest_url!r} must be an http(s) URL")
        if api_manifest_url and api_manifest_url.startswith("http://") and not api_allow_insecure_http:
            errors.append(
                "TUVA_API_MANIFEST_URL uses plain HTTP but TUVA_API_ALLOW_INSECURE_HTTP is not "
                "enabled; HTTPS is required by default (set TUVA_API_ALLOW_INSECURE_HTTP=1 only "
                "for local tests against a mock server)"
            )
        if "api_token" in required and not api_token:
            errors.append("TUVA_API_TOKEN is required but not set")

        if api_timeout_seconds <= 0:
            errors.append(f"TUVA_API_TIMEOUT_SECONDS must be > 0, got {api_timeout_seconds}")
        if api_max_retries < 0:
            errors.append(f"TUVA_API_MAX_RETRIES must be >= 0, got {api_max_retries}")

        if "raw_data_dir" in required:
            if not raw_data_dir_raw.strip():
                errors.append("RAW_DATA_DIR is required but not set")
            elif str(raw_data_dir) in ("/", "."):
                errors.append(f"RAW_DATA_DIR={raw_data_dir_raw!r} is not a safe pipeline data directory")

        if "pg_dsn" in required and not pg_dsn:
            errors.append("PG_DSN is required but not set")

        _validate_identifier("PG_SCHEMA", pg_schema, errors)
        _validate_identifier("TERMINOLOGY_SCHEMA", terminology_schema, errors)
        _validate_identifier("OPS_SCHEMA", ops_schema, errors)
        if ops_schema == pg_schema:
            errors.append("OPS_SCHEMA must differ from PG_SCHEMA (operational tables must not mix with data tables)")

        if pipeline_max_success_age_hours <= 0:
            errors.append(
                f"PIPELINE_MAX_SUCCESS_AGE_HOURS must be > 0, got {pipeline_max_success_age_hours}"
            )

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in valid_levels:
            errors.append(f"LOG_LEVEL={log_level!r} must be one of {sorted(valid_levels)}")

        if errors:
            raise ConfigError(
                f"{len(errors)} configuration problem(s) found:\n  - " + "\n  - ".join(errors)
            )

        return cls(
            api_manifest_url=api_manifest_url,
            api_token=api_token,
            api_timeout_seconds=api_timeout_seconds,
            api_max_retries=api_max_retries,
            api_allow_insecure_http=api_allow_insecure_http,
            raw_data_dir=raw_data_dir,
            pg_dsn=pg_dsn,
            pg_schema=pg_schema,
            terminology_schema=terminology_schema,
            ops_schema=ops_schema,
            pipeline_environment=pipeline_environment,
            pipeline_max_success_age_hours=pipeline_max_success_age_hours,
            log_level=log_level,
            metrics_file=metrics_file,
        )

    def safe_dict(self) -> dict:
        """A dict safe to log or print -- secrets are redacted, not omitted,
        so operators can still see *that* a value is set."""
        return {
            "api_manifest_url": self.api_manifest_url,
            "api_token": "***REDACTED***" if self.api_token else None,
            "api_timeout_seconds": self.api_timeout_seconds,
            "api_max_retries": self.api_max_retries,
            "api_allow_insecure_http": self.api_allow_insecure_http,
            "raw_data_dir": str(self.raw_data_dir),
            "pg_dsn": "***REDACTED***" if self.pg_dsn else None,
            "pg_schema": self.pg_schema,
            "terminology_schema": self.terminology_schema,
            "ops_schema": self.ops_schema,
            "pipeline_environment": self.pipeline_environment,
            "pipeline_max_success_age_hours": self.pipeline_max_success_age_hours,
            "log_level": self.log_level,
            "metrics_file": str(self.metrics_file) if self.metrics_file else None,
        }

    def __repr__(self) -> str:  # never let a stray print() leak secrets
        return f"PipelineConfig({self.safe_dict()!r})"
