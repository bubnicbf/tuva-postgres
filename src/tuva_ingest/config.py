"""Environment-driven configuration for the ingestion connector.

Every deployment setting is an environment variable (see
scripts/setup_env.example for the full list with non-secret examples).
`IngestConfig.load()` fails fast with a single, clear `ConfigError`
listing every problem found, rather than raising on the first one -- an
operator fixing a broken `.env` should see everything wrong at once.

Secrets (`api_token`, `pg_dsn`) are never included in `repr()`/`str()` or
in any log line. Use `IngestConfig.safe_dict()` for anything that might
be logged.

This connector never targets Tuva-managed core/terminology/output
schemas directly -- `raw_schema` (source data) and `ops_schema`
(operational/control metadata) are the only two schemas this Python
package ever writes to. `input_layer_schema` and `dbt_target`/
`dbt_profiles_dir` exist only so the CLI can shell out to `dbt` with the
right target/vars; this package never issues SQL against the Input Layer
or Tuva package schemas itself (see cli.py's `dbt` subcommand).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ConfigError
from .identifiers import InvalidIdentifierError, validate_identifier

# The full set of fields a given CLI command needs populated. Commands
# that don't touch the API (e.g. `migrate`, `healthcheck`) shouldn't fail
# because TUVA_API_TOKEN is unset.
REQUIRE_API = frozenset({"api_manifest_url", "api_token"})
REQUIRE_DB = frozenset({"pg_dsn"})
REQUIRE_RAW_DATA = frozenset({"raw_data_dir"})

ALL_REQUIREMENTS = REQUIRE_API | REQUIRE_DB | REQUIRE_RAW_DATA


def _validate_identifier(name: str, value: str, errors: list[str]) -> None:
    """Validate `value` against the shared identifier policy (see
    identifiers.py), appending a message to `errors` instead of raising --
    `IngestConfig.load()` collects every configuration problem at once
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
class IngestConfig:
    api_manifest_url: str | None
    api_token: str | None
    api_timeout_seconds: float
    api_max_retries: int
    api_allow_insecure_http: bool

    raw_data_dir: Path

    pg_dsn: str | None
    raw_schema: str
    ops_schema: str

    # dbt-facing configuration. This package never connects to these
    # schemas/targets directly -- it only ever passes them through to
    # `dbt` as `--target`/`--vars` (see cli.py's dbt subcommand) so a
    # single .env is the one source of truth for both the Python
    # connector and the dbt project.
    input_layer_schema: str
    dbt_target: str
    dbt_profiles_dir: Path
    dbt_project_dir: Path

    pipeline_environment: str
    pipeline_max_success_age_hours: float

    log_level: str

    source_name: str = field(default="tuva")

    # Role names used by migrations/003_roles_and_grants.sql for
    # least-privilege grants (ingest_role: INSERT/TRUNCATE/SELECT on the
    # raw schema + read/write on ops_schema; transform_role: SELECT-only
    # on the raw schema, for dbt). Always concrete (never None) so
    # 003_roles_and_grants.sql's rendered SQL -- and therefore its
    # checksum-tracked idempotency -- is deterministic for a given
    # configuration; override via INGEST_ROLE/TRANSFORM_ROLE for a
    # multi-role production deployment.
    ingest_role: str = field(default="tuva_ingest_role")
    transform_role: str = field(default="tuva_transform_role")

    @classmethod
    def load(cls, required: frozenset[str] = ALL_REQUIREMENTS) -> "IngestConfig":
        errors: list[str] = []

        api_manifest_url = os.environ.get("TUVA_API_MANIFEST_URL") or None
        api_token = os.environ.get("TUVA_API_TOKEN") or None
        api_timeout_seconds = _env_float("TUVA_API_TIMEOUT_SECONDS", 30.0, errors)
        api_max_retries = _env_int("TUVA_API_MAX_RETRIES", 5, errors)
        api_allow_insecure_http = _env_bool("TUVA_API_ALLOW_INSECURE_HTTP", False)

        raw_data_dir_raw = os.environ.get("RAW_DATA_DIR", "data/raw")
        raw_data_dir = Path(raw_data_dir_raw)

        pg_dsn = os.environ.get("PG_DSN") or None
        raw_schema = os.environ.get("RAW_SCHEMA", "raw")
        ops_schema = os.environ.get("OPS_SCHEMA", "ingest_ops")

        input_layer_schema = os.environ.get("INPUT_LAYER_SCHEMA", "input_layer")
        dbt_target = os.environ.get("DBT_TARGET", "dev")
        dbt_profiles_dir = Path(os.environ.get("DBT_PROFILES_DIR", "."))
        dbt_project_dir = Path(os.environ.get("DBT_PROJECT_DIR", "."))

        pipeline_environment = os.environ.get("PIPELINE_ENVIRONMENT", "local")
        pipeline_max_success_age_hours = _env_float("PIPELINE_MAX_SUCCESS_AGE_HOURS", 30.0, errors)

        log_level = os.environ.get("LOG_LEVEL", "INFO").upper()

        source_name = os.environ.get("SOURCE_NAME", "tuva")

        ingest_role = os.environ.get("INGEST_ROLE", "tuva_ingest_role")
        transform_role = os.environ.get("TRANSFORM_ROLE", "tuva_transform_role")

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
                errors.append(f"RAW_DATA_DIR={raw_data_dir_raw!r} is not a safe extraction data directory")

        if "pg_dsn" in required and not pg_dsn:
            errors.append("PG_DSN is required but not set")

        _validate_identifier("RAW_SCHEMA", raw_schema, errors)
        _validate_identifier("OPS_SCHEMA", ops_schema, errors)
        _validate_identifier("INPUT_LAYER_SCHEMA", input_layer_schema, errors)
        if ops_schema == raw_schema:
            errors.append("OPS_SCHEMA must differ from RAW_SCHEMA (operational tables must not mix with raw data)")
        if input_layer_schema == raw_schema:
            errors.append(
                "INPUT_LAYER_SCHEMA must differ from RAW_SCHEMA (dbt's Input Layer/staging output must "
                "not be written into the raw landing schema)"
            )

        if ingest_role is not None:
            _validate_identifier("INGEST_ROLE", ingest_role, errors)
        if transform_role is not None:
            _validate_identifier("TRANSFORM_ROLE", transform_role, errors)
        if ingest_role is not None and transform_role is not None and ingest_role == transform_role:
            errors.append("INGEST_ROLE and TRANSFORM_ROLE must differ (least-privilege role separation)")

        if pipeline_max_success_age_hours <= 0:
            errors.append(
                f"PIPELINE_MAX_SUCCESS_AGE_HOURS must be > 0, got {pipeline_max_success_age_hours}"
            )

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if log_level not in valid_levels:
            errors.append(f"LOG_LEVEL={log_level!r} must be one of {sorted(valid_levels)}")

        if not source_name.strip():
            errors.append("SOURCE_NAME must not be empty")

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
            raw_schema=raw_schema,
            ops_schema=ops_schema,
            input_layer_schema=input_layer_schema,
            dbt_target=dbt_target,
            dbt_profiles_dir=dbt_profiles_dir,
            dbt_project_dir=dbt_project_dir,
            pipeline_environment=pipeline_environment,
            pipeline_max_success_age_hours=pipeline_max_success_age_hours,
            log_level=log_level,
            source_name=source_name,
            ingest_role=ingest_role,
            transform_role=transform_role,
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
            "raw_schema": self.raw_schema,
            "ops_schema": self.ops_schema,
            "input_layer_schema": self.input_layer_schema,
            "dbt_target": self.dbt_target,
            "dbt_profiles_dir": str(self.dbt_profiles_dir),
            "dbt_project_dir": str(self.dbt_project_dir),
            "pipeline_environment": self.pipeline_environment,
            "pipeline_max_success_age_hours": self.pipeline_max_success_age_hours,
            "log_level": self.log_level,
            "source_name": self.source_name,
            "ingest_role": self.ingest_role,
            "transform_role": self.transform_role,
        }

    def __repr__(self) -> str:  # never let a stray print() leak secrets
        return f"IngestConfig({self.safe_dict()!r})"
