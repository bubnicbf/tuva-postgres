"""Environment-driven configuration for the ingestion connector, built on
`pydantic-settings` (`IngestConfig(BaseSettings)`).

Every deployment setting is an environment variable (see
scripts/setup_env.example for the full list with non-secret examples),
loaded the same way `pydantic-settings` always does: real process
environment variables first, then (if present) a `.env` file in the
current working directory (git-ignored; `make init` copies
`scripts/setup_env.example` there for you), then the field's own
default. `IngestConfig.load()` fails fast with a single, clear
`ConfigError` listing every problem found at once -- an operator fixing
a broken `.env` should see everything wrong in one pass, not one error
per retry.

Secrets (`api_token`, `pg_dsn`) are represented as `pydantic.SecretStr`,
never a plain `str` -- `repr()`/`str()`/pydantic's own validation-error
rendering can never accidentally include the underlying value (SecretStr
always renders as `SecretStr('**********')`). Call sites that need the
real value for an actual HTTP request or database connection call
`.api_token_value` / `.pg_dsn_value` (or `.get_secret_value()` directly)
explicitly, at the one point they need it -- never in anything that
might be logged or printed. `IngestConfig.safe_dict()` is what to use
for anything that *might* be logged.

Every field a given CLI command doesn't need stays `None`/its default
rather than failing at parse time -- `IngestConfig.load(required=...)`
is what enforces "this command needs TUVA_API_TOKEN set" on top of
`pydantic-settings`' own type/constraint validation, so e.g. `tuva-ingest
migrate` never fails because `TUVA_API_TOKEN` is unset (see
`REQUIRE_API`/`REQUIRE_DB`/`REQUIRE_RAW_DATA` below and each CLI
subcommand's own `required=` argument to `IngestConfig.load()`).

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
from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError
from .identifiers import InvalidIdentifierError, validate_identifier

# The full set of fields a given CLI command needs populated. Commands
# that don't touch the API (e.g. `migrate`, `healthcheck`) shouldn't fail
# because TUVA_API_TOKEN is unset.
REQUIRE_API = frozenset({"api_manifest_url", "api_token"})
REQUIRE_DB = frozenset({"pg_dsn"})
REQUIRE_RAW_DATA = frozenset({"raw_data_dir"})

ALL_REQUIREMENTS = REQUIRE_API | REQUIRE_DB | REQUIRE_RAW_DATA

# The paginated extract/load/sync commands (see cli.py, pagination.py,
# paginated_loader.py) retrieve their API credential from the configured
# secret provider (see secrets.py) rather than requiring TUVA_API_TOKEN
# directly -- so, unlike REQUIRE_API above, this set deliberately omits
# "api_token". They do need TUVA_API_MANIFEST_URL (reused as the page-
# request URL), RAW_DATA_DIR (immutable page-file staging/publish), and
# PG_DSN (watermark lookups plus the load/reconcile/commit transaction).
REQUIRE_PAGINATED = frozenset({"api_manifest_url", "raw_data_dir", "pg_dsn"})

# The object-storage-backed extract/load/sync workflow (see
# object_extract.py/object_raw_loader.py, cli.py's `--storage
# object-storage`) never requires RAW_DATA_DIR (it publishes to object
# storage, not a local directory) -- see REQUIRE_PAGINATED above for the
# filesystem-backed equivalent this parallels.
REQUIRE_OBJECT_STORAGE = frozenset({"api_manifest_url", "pg_dsn"})

_LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

# Single source of truth mapping each pydantic field name to the exact
# environment variable name it is bound to (preserving every existing
# variable name from the pre-pydantic dataclass implementation, so
# deployments/.env files never need to change). Field declarations below
# reference this mapping rather than repeating the string literal, and
# `_format_pydantic_error` uses it in reverse to translate a pydantic
# validation error's field name back into the env var an operator
# actually set.
_ENV_ALIASES: dict[str, str] = {
    "api_manifest_url": "TUVA_API_MANIFEST_URL",
    "api_token": "TUVA_API_TOKEN",
    "api_timeout_seconds": "TUVA_API_TIMEOUT_SECONDS",
    "api_max_retries": "TUVA_API_MAX_RETRIES",
    "api_allow_insecure_http": "TUVA_API_ALLOW_INSECURE_HTTP",
    "api_connect_timeout_seconds": "TUVA_API_CONNECT_TIMEOUT_SECONDS",
    "api_read_timeout_seconds": "TUVA_API_READ_TIMEOUT_SECONDS",
    "api_write_timeout_seconds": "TUVA_API_WRITE_TIMEOUT_SECONDS",
    "api_pool_timeout_seconds": "TUVA_API_POOL_TIMEOUT_SECONDS",
    "api_max_retry_delay_seconds": "TUVA_API_MAX_RETRY_DELAY_SECONDS",
    "raw_data_dir": "RAW_DATA_DIR",
    "pg_dsn": "PG_DSN",
    "raw_schema": "RAW_SCHEMA",
    "ops_schema": "OPS_SCHEMA",
    "input_layer_schema": "INPUT_LAYER_SCHEMA",
    "dbt_target": "DBT_TARGET",
    "dbt_profiles_dir": "DBT_PROFILES_DIR",
    "dbt_project_dir": "DBT_PROJECT_DIR",
    "pipeline_environment": "PIPELINE_ENVIRONMENT",
    "pipeline_max_success_age_hours": "PIPELINE_MAX_SUCCESS_AGE_HOURS",
    "log_level": "LOG_LEVEL",
    "source_name": "SOURCE_NAME",
    "ingest_role": "INGEST_ROLE",
    "transform_role": "TRANSFORM_ROLE",
    "api_secret_provider": "TUVA_API_SECRET_PROVIDER",
    "api_secret_id": "TUVA_API_SECRET_ID",
    "aws_region": "AWS_REGION",
    "api_page_size": "TUVA_API_PAGE_SIZE",
    "api_max_pages": "TUVA_API_MAX_PAGES",
    "api_max_page_bytes": "TUVA_API_MAX_PAGE_BYTES",
    "staging_schema": "STAGING_SCHEMA",
    "analytics_core_schema": "ANALYTICS_CORE_SCHEMA",
    "analytics_marts_schema": "ANALYTICS_MARTS_SCHEMA",
    "object_storage_provider": "OBJECT_STORAGE_PROVIDER",
    "object_storage_bucket": "OBJECT_STORAGE_BUCKET",
    "object_storage_prefix": "OBJECT_STORAGE_PREFIX",
    "object_storage_region": "OBJECT_STORAGE_REGION",
    "object_storage_endpoint_url": "OBJECT_STORAGE_ENDPOINT_URL",
    "object_storage_local_root": "OBJECT_STORAGE_LOCAL_ROOT",
}


def _identifier_field_validator(env_name: str):
    """Build a pydantic `field_validator` function that validates a schema/
    role identifier field against the shared `identifiers.py` policy,
    translating `InvalidIdentifierError` into a plain `ValueError` (which
    is what pydantic field validators are expected to raise) tagged with
    the env var name so the resulting message stays actionable."""

    def _validate(value: str) -> str:
        try:
            return validate_identifier(value, env_name)
        except InvalidIdentifierError as exc:
            raise ValueError(str(exc)) from exc

    return _validate


def _format_pydantic_error(err: dict[str, Any]) -> str:
    """Translate one entry of `pydantic.ValidationError.errors()` into an
    operator-actionable message that always names the *environment
    variable* the value came from (never pydantic's internal Python
    field/attribute name, which an operator setting `.env` values has
    never seen)."""
    loc = err.get("loc") or ()
    field_name = str(loc[0]) if loc else "<config>"
    env_name = _ENV_ALIASES.get(field_name, field_name)
    message = err.get("msg", "is invalid")
    given = err.get("input", None)
    return f"{env_name}={given!r} {message}"


class IngestConfig(BaseSettings):
    """Environment-driven, validated ingestion connector configuration.

    Construct via `IngestConfig.load(required=...)` (never the bare
    pydantic constructor directly) so command-scoped required-field
    checks and the legacy, actionable `ConfigError` message format are
    applied consistently everywhere the CLI loads configuration.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=True,
        validate_default=True,
        frozen=True,
    )

    api_manifest_url: str | None = Field(default=None, validation_alias=_ENV_ALIASES["api_manifest_url"])
    api_token: SecretStr | None = Field(default=None, validation_alias=_ENV_ALIASES["api_token"])
    api_timeout_seconds: float = Field(default=30.0, gt=0, validation_alias=_ENV_ALIASES["api_timeout_seconds"])
    api_max_retries: int = Field(default=5, ge=0, validation_alias=_ENV_ALIASES["api_max_retries"])
    api_allow_insecure_http: bool = Field(default=False, validation_alias=_ENV_ALIASES["api_allow_insecure_http"])

    # Explicit per-phase httpx timeouts (see http_client.py). Each
    # defaults to `None`, meaning "fall back to api_timeout_seconds" --
    # see `httpx_timeout()` below -- so a deployment that only sets
    # TUVA_API_TIMEOUT_SECONDS (the pre-existing variable) keeps working
    # unchanged, while one that needs asymmetric bounds (e.g. a slow
    # connect but fast read) can set the phase-specific variables too.
    api_connect_timeout_seconds: float | None = Field(
        default=None, gt=0, validation_alias=_ENV_ALIASES["api_connect_timeout_seconds"]
    )
    api_read_timeout_seconds: float | None = Field(
        default=None, gt=0, validation_alias=_ENV_ALIASES["api_read_timeout_seconds"]
    )
    api_write_timeout_seconds: float | None = Field(
        default=None, gt=0, validation_alias=_ENV_ALIASES["api_write_timeout_seconds"]
    )
    api_pool_timeout_seconds: float | None = Field(
        default=None, gt=0, validation_alias=_ENV_ALIASES["api_pool_timeout_seconds"]
    )
    # Hard ceiling on any single retry sleep (bounded exponential backoff
    # with jitter, and on a valid Retry-After value) -- see
    # http_client.py's `_wait_strategy`. Retries are always bounded by
    # api_max_retries *and* every individual sleep is bounded by this
    # value; neither bound can be disabled.
    api_max_retry_delay_seconds: float = Field(
        default=30.0, gt=0, validation_alias=_ENV_ALIASES["api_max_retry_delay_seconds"]
    )

    raw_data_dir: Path = Field(default=Path("data/raw"), validation_alias=_ENV_ALIASES["raw_data_dir"])

    pg_dsn: SecretStr | None = Field(default=None, validation_alias=_ENV_ALIASES["pg_dsn"])
    # Six-schema lineage (see docs/SOURCE_CONTRACT.md "Schema lineage" and
    # README.md "Architecture"): ops (pipeline state/audit) -> raw_incoming
    # (this connector's own landing tables) -> staging_incoming (dbt
    # staging models) -> input_layer (this project's Input Layer contract)
    # -> analytics_core / analytics_marts (the pinned Tuva package's own
    # core/mart outputs -- see macros/generate_schema_name.sql). Defaults
    # changed from the pre-object-storage values (`raw`/`ingest_ops`) to
    # these names as part of this same change -- an EXISTING deployment
    # that never explicitly set RAW_SCHEMA/OPS_SCHEMA must set them
    # explicitly to its current value (`raw`/`ingest_ops`) to keep reading
    # its existing data; see docs/RUNBOOK.md "Upgrade notes" and
    # migrations/006_object_storage_raw_contract.sql's own comment on this.
    # An override of either variable continues to work exactly as before
    # (backward-compatible) -- only the *default* changed.
    raw_schema: str = Field(default="raw_incoming", validation_alias=_ENV_ALIASES["raw_schema"])
    ops_schema: str = Field(default="ops", validation_alias=_ENV_ALIASES["ops_schema"])

    # dbt-facing configuration. This package never connects to these
    # schemas/targets directly -- it only ever passes them through to
    # `dbt` as `--target`/`--vars` (see cli.py's dbt subcommand) so a
    # single .env is the one source of truth for both the Python
    # connector and the dbt project.
    staging_schema: str = Field(default="staging_incoming", validation_alias=_ENV_ALIASES["staging_schema"])
    input_layer_schema: str = Field(default="input_layer", validation_alias=_ENV_ALIASES["input_layer_schema"])
    # Routes the pinned Tuva package's own core/mart model output (never
    # this connector's own models -- see macros/generate_schema_name.sql)
    # into these two schemas instead of whatever the_tuva_project's own
    # dbt_project.yml would otherwise name them.
    analytics_core_schema: str = Field(
        default="analytics_core", validation_alias=_ENV_ALIASES["analytics_core_schema"]
    )
    analytics_marts_schema: str = Field(
        default="analytics_marts", validation_alias=_ENV_ALIASES["analytics_marts_schema"]
    )
    dbt_target: str = Field(default="dev", validation_alias=_ENV_ALIASES["dbt_target"])
    dbt_profiles_dir: Path = Field(default=Path("."), validation_alias=_ENV_ALIASES["dbt_profiles_dir"])
    dbt_project_dir: Path = Field(default=Path("."), validation_alias=_ENV_ALIASES["dbt_project_dir"])

    pipeline_environment: str = Field(default="local", validation_alias=_ENV_ALIASES["pipeline_environment"])
    pipeline_max_success_age_hours: float = Field(
        default=30.0, gt=0, validation_alias=_ENV_ALIASES["pipeline_max_success_age_hours"]
    )

    log_level: str = Field(default="INFO", validation_alias=_ENV_ALIASES["log_level"])

    source_name: str = Field(default="tuva", validation_alias=_ENV_ALIASES["source_name"])

    # Role names used by migrations/003_roles_and_grants.sql for
    # least-privilege grants (ingest_role: INSERT/TRUNCATE/SELECT on the
    # raw schema + read/write on ops_schema; transform_role: SELECT-only
    # on the raw schema, for dbt). Always concrete (never None) so
    # 003_roles_and_grants.sql's rendered SQL -- and therefore its
    # checksum-tracked idempotency -- is deterministic for a given
    # configuration; override via INGEST_ROLE/TRANSFORM_ROLE for a
    # multi-role production deployment.
    ingest_role: str = Field(default="tuva_ingest_role", validation_alias=_ENV_ALIASES["ingest_role"])
    transform_role: str = Field(default="tuva_transform_role", validation_alias=_ENV_ALIASES["transform_role"])

    # --- cloud secret manager (see secrets.py) --------------------------
    # Non-secret lookup information only -- the credential itself is
    # always retrieved at runtime from the configured provider, never
    # read from a plaintext env var here (except the "env" provider,
    # which *is* TUVA_API_TOKEN by design -- see secrets.EnvSecretProvider
    # and this field's own default, kept for full backward compatibility
    # with every existing local/CI/test workflow).
    api_secret_provider: str = Field(default="env", validation_alias=_ENV_ALIASES["api_secret_provider"])
    api_secret_id: str | None = Field(default=None, validation_alias=_ENV_ALIASES["api_secret_id"])
    aws_region: str | None = Field(default=None, validation_alias=_ENV_ALIASES["aws_region"])

    # --- paginated extraction (see pagination.py) -----------------------
    api_page_size: int | None = Field(default=None, gt=0, validation_alias=_ENV_ALIASES["api_page_size"])
    # Hard ceiling on pagination loop iterations -- the final defense
    # against an infinite pagination loop (a source that never returns a
    # null next_page_token, or a pagination cycle this connector's own
    # repeated-token detection somehow missed).
    api_max_pages: int = Field(default=10_000, gt=0, validation_alias=_ENV_ALIASES["api_max_pages"])
    # Per-page response size ceiling (mirrors api_client.MAX_MANIFEST_BYTES'
    # role for the manifest contract, sized larger since a page of JSON
    # records is expected to be larger than a manifest document).
    api_max_page_bytes: int = Field(
        default=64 * 1024 * 1024, gt=0, validation_alias=_ENV_ALIASES["api_max_page_bytes"]
    )

    # --- object storage (see object_storage/, object_extract.py, object_raw_loader.py) ---
    # The durable, immutable, replayable source of truth for extracted
    # source pages (see docs/SOURCE_CONTRACT.md "Object storage").
    # "local" (default) uses object_storage.local.LocalFilesystemBackend
    # -- real file I/O, but not a production object store -- so the
    # object-storage code path is exercisable in local development and
    # CI without any cloud credentials or a running MinIO container.
    # "s3" uses object_storage.s3.S3Backend against real AWS S3 or any
    # S3-compatible endpoint (set OBJECT_STORAGE_ENDPOINT_URL for MinIO).
    # Deliberately never a static access-key/secret-key setting here --
    # see object_storage/s3.py's module docstring: authentication is
    # always boto3's ambient credential chain (an IAM role, an assumed
    # role, AWS_PROFILE, or a local developer profile).
    object_storage_provider: str = Field(default="local", validation_alias=_ENV_ALIASES["object_storage_provider"])
    object_storage_bucket: str | None = Field(default=None, validation_alias=_ENV_ALIASES["object_storage_bucket"])
    # The key prefix documented in docs/SOURCE_CONTRACT.md's object-key
    # convention (`<prefix>/vendor=.../endpoint=.../load_date=.../
    # run_id=.../page=......jsonl.gz`) -- configurable while keeping
    # "raw" as the default, per that same contract.
    object_storage_prefix: str = Field(
        default="raw", validation_alias=_ENV_ALIASES["object_storage_prefix"]
    )
    object_storage_region: str | None = Field(
        default=None, validation_alias=_ENV_ALIASES["object_storage_region"]
    )
    # Only meaningful for OBJECT_STORAGE_PROVIDER=s3 -- a custom
    # S3-compatible endpoint (e.g. http://localhost:9000 for the local
    # MinIO container in compose.yml). Left unset (None) to use real AWS
    # S3's own regional endpoints.
    object_storage_endpoint_url: str | None = Field(
        default=None, validation_alias=_ENV_ALIASES["object_storage_endpoint_url"]
    )
    # Only meaningful for OBJECT_STORAGE_PROVIDER=local -- the local
    # filesystem root object_storage.local.LocalFilesystemBackend writes
    # under. Kept separate from RAW_DATA_DIR (the legacy/local-paginated
    # contract's own directory layout, see pagination.py) so the two
    # storage layouts can never collide on disk.
    object_storage_local_root: Path = Field(
        default=Path("data/object_storage"), validation_alias=_ENV_ALIASES["object_storage_local_root"]
    )

    # --- field-level validators (always run, regardless of `required`) ---

    @field_validator("raw_schema")
    @classmethod
    def _check_raw_schema(cls, value: str) -> str:
        return _identifier_field_validator("RAW_SCHEMA")(value)

    @field_validator("ops_schema")
    @classmethod
    def _check_ops_schema(cls, value: str) -> str:
        return _identifier_field_validator("OPS_SCHEMA")(value)

    @field_validator("input_layer_schema")
    @classmethod
    def _check_input_layer_schema(cls, value: str) -> str:
        return _identifier_field_validator("INPUT_LAYER_SCHEMA")(value)

    @field_validator("staging_schema")
    @classmethod
    def _check_staging_schema(cls, value: str) -> str:
        return _identifier_field_validator("STAGING_SCHEMA")(value)

    @field_validator("analytics_core_schema")
    @classmethod
    def _check_analytics_core_schema(cls, value: str) -> str:
        return _identifier_field_validator("ANALYTICS_CORE_SCHEMA")(value)

    @field_validator("analytics_marts_schema")
    @classmethod
    def _check_analytics_marts_schema(cls, value: str) -> str:
        return _identifier_field_validator("ANALYTICS_MARTS_SCHEMA")(value)

    @field_validator("object_storage_provider")
    @classmethod
    def _check_object_storage_provider(cls, value: str) -> str:
        from .object_storage.factory import SUPPORTED_PROVIDERS

        if value not in SUPPORTED_PROVIDERS:
            raise ValueError(f"must be one of {sorted(SUPPORTED_PROVIDERS)}")
        return value

    @field_validator("object_storage_prefix")
    @classmethod
    def _check_object_storage_prefix(cls, value: str) -> str:
        from .object_storage.keys import validate_prefix
        from .errors import ObjectKeyError

        try:
            return validate_prefix(value)
        except ObjectKeyError as exc:
            raise ValueError(str(exc)) from exc

    @field_validator("ingest_role")
    @classmethod
    def _check_ingest_role(cls, value: str) -> str:
        return _identifier_field_validator("INGEST_ROLE")(value)

    @field_validator("transform_role")
    @classmethod
    def _check_transform_role(cls, value: str) -> str:
        return _identifier_field_validator("TRANSFORM_ROLE")(value)

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("log_level")
    @classmethod
    def _check_log_level(cls, value: str) -> str:
        if value not in _LOG_LEVELS:
            raise ValueError(f"must be one of {sorted(_LOG_LEVELS)}")
        return value

    @field_validator("source_name")
    @classmethod
    def _check_source_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value

    @field_validator("api_secret_provider")
    @classmethod
    def _check_api_secret_provider(cls, value: str) -> str:
        from .secrets import SUPPORTED_SECRET_PROVIDERS

        if value not in SUPPORTED_SECRET_PROVIDERS:
            raise ValueError(f"must be one of {sorted(SUPPORTED_SECRET_PROVIDERS)}")
        return value

    @field_validator("api_manifest_url")
    @classmethod
    def _check_manifest_url_scheme(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith(("https://", "http://")):
            raise ValueError("must be an http(s) URL")
        return value

    @field_validator(
        "api_timeout_seconds",
        "api_connect_timeout_seconds",
        "api_read_timeout_seconds",
        "api_write_timeout_seconds",
        "api_pool_timeout_seconds",
        "api_max_retry_delay_seconds",
        "pipeline_max_success_age_hours",
    )
    @classmethod
    def _check_positive_float(cls, value: float | None) -> float | None:
        # pydantic's own `gt=0` constraint already enforces this; this
        # validator exists only so a non-numeric value (e.g.
        # TUVA_API_TIMEOUT_SECONDS=not-a-number) fails with an unambiguous
        # message rather than a bare pydantic type-coercion error --
        # pydantic already raises before this validator runs for a
        # genuinely non-numeric string, so this is a light no-op pass-
        # through kept for clarity/documentation of intent.
        return value

    # --- cross-field validation (always run, regardless of `required`) ---

    @model_validator(mode="after")
    def _check_cross_field_rules(self) -> "IngestConfig":
        errors: list[str] = []

        if self.api_manifest_url and self.api_manifest_url.startswith("http://") and not self.api_allow_insecure_http:
            errors.append(
                "TUVA_API_MANIFEST_URL uses plain HTTP but TUVA_API_ALLOW_INSECURE_HTTP is not "
                "enabled; HTTPS is required by default (set TUVA_API_ALLOW_INSECURE_HTTP=1 only "
                "for local tests against a mock server)"
            )

        if self.ops_schema == self.raw_schema:
            errors.append("OPS_SCHEMA must differ from RAW_SCHEMA (operational tables must not mix with raw data)")
        if self.input_layer_schema == self.raw_schema:
            errors.append(
                "INPUT_LAYER_SCHEMA must differ from RAW_SCHEMA (dbt's Input Layer/staging output must "
                "not be written into the raw landing schema)"
            )
        if self.ingest_role == self.transform_role:
            errors.append("INGEST_ROLE and TRANSFORM_ROLE must differ (least-privilege role separation)")

        if self.api_secret_provider == "aws" and not self.api_secret_id:
            errors.append(
                "TUVA_API_SECRET_PROVIDER=aws requires TUVA_API_SECRET_ID to be set (the AWS Secrets "
                "Manager secret name or ARN to retrieve -- see secrets.py)"
            )

        # Six-schema lineage: every schema this connector or dbt writes
        # into must be pairwise distinct, or two logically different
        # layers (e.g. raw landing data and Tuva's own core outputs)
        # could silently share one physical schema.
        six_schemas = {
            "OPS_SCHEMA": self.ops_schema,
            "RAW_SCHEMA": self.raw_schema,
            "STAGING_SCHEMA": self.staging_schema,
            "INPUT_LAYER_SCHEMA": self.input_layer_schema,
            "ANALYTICS_CORE_SCHEMA": self.analytics_core_schema,
            "ANALYTICS_MARTS_SCHEMA": self.analytics_marts_schema,
        }
        seen: dict[str, str] = {}
        for env_name, value in six_schemas.items():
            if value in seen:
                errors.append(f"{env_name} and {seen[value]} must not both be {value!r} (six distinct schemas are required)")
            else:
                seen[value] = env_name

        if self.object_storage_provider == "s3" and not self.object_storage_bucket:
            errors.append(
                "OBJECT_STORAGE_PROVIDER=s3 requires OBJECT_STORAGE_BUCKET to be set"
            )

        if errors:
            raise ValueError("; ".join(errors))
        return self

    # --- construction entry point ------------------------------------

    @classmethod
    def load(cls, required: frozenset[str] = ALL_REQUIREMENTS) -> "IngestConfig":
        """The one supported way to build an `IngestConfig`: reads real
        environment variables (then `.env`, then defaults -- standard
        `pydantic-settings` precedence), validates every field via
        pydantic (types, ranges, identifiers, cross-field rules -- always
        active), and additionally enforces that every field named in
        `required` is actually set (command-scoped -- see
        REQUIRE_API/REQUIRE_DB/REQUIRE_RAW_DATA). Every problem found is
        raised together in a single `ConfigError`, never one at a time.
        """
        errors: list[str] = []

        if "api_manifest_url" in required and not os.environ.get("TUVA_API_MANIFEST_URL"):
            errors.append("TUVA_API_MANIFEST_URL is required but not set")
        if "api_token" in required and not os.environ.get("TUVA_API_TOKEN"):
            errors.append("TUVA_API_TOKEN is required but not set")
        if "pg_dsn" in required and not os.environ.get("PG_DSN"):
            errors.append("PG_DSN is required but not set")
        if "raw_data_dir" in required:
            raw_data_dir_raw = os.environ.get("RAW_DATA_DIR", "data/raw")
            if not raw_data_dir_raw.strip():
                errors.append("RAW_DATA_DIR is required but not set")
            elif Path(raw_data_dir_raw) in (Path("/"), Path(".")):
                errors.append(f"RAW_DATA_DIR={raw_data_dir_raw!r} is not a safe extraction data directory")

        settings: IngestConfig | None = None
        try:
            settings = cls()
        except ValidationError as exc:
            for err in exc.errors():
                errors.append(_format_pydantic_error(err))

        if errors:
            raise ConfigError(f"{len(errors)} configuration problem(s) found:\n  - " + "\n  - ".join(errors))

        assert settings is not None  # no errors means construction succeeded above
        return settings

    # --- secret access (explicit, at the point of use only) --------------

    @property
    def api_token_value(self) -> str | None:
        """The real bearer token, unwrapped. Call only at the point an
        actual HTTP request is built (http_client.py) -- never store the
        return value anywhere that might be logged."""
        return self.api_token.get_secret_value() if self.api_token else None

    @property
    def pg_dsn_value(self) -> str | None:
        """The real PostgreSQL DSN, unwrapped. Call only at the point a
        real connection is opened (db.py's `connect`) -- never store the
        return value anywhere that might be logged."""
        return self.pg_dsn.get_secret_value() if self.pg_dsn else None

    def httpx_timeout(self):
        """Build an `httpx.Timeout` with explicit connect/read/write/pool
        bounds -- each phase-specific env var overrides
        `api_timeout_seconds` for that one phase; unset phases fall back
        to `api_timeout_seconds` (see http_client.py's `ApiClient`)."""
        import httpx

        return httpx.Timeout(
            connect=self.api_connect_timeout_seconds or self.api_timeout_seconds,
            read=self.api_read_timeout_seconds or self.api_timeout_seconds,
            write=self.api_write_timeout_seconds or self.api_timeout_seconds,
            pool=self.api_pool_timeout_seconds or self.api_timeout_seconds,
        )

    # --- safe representations -------------------------------------------

    def safe_dict(self) -> dict:
        """A dict safe to log or print -- secrets are redacted, not omitted,
        so operators can still see *that* a value is set."""
        return {
            "api_manifest_url": self.api_manifest_url,
            "api_token": "***REDACTED***" if self.api_token else None,
            "api_timeout_seconds": self.api_timeout_seconds,
            "api_max_retries": self.api_max_retries,
            "api_allow_insecure_http": self.api_allow_insecure_http,
            "api_connect_timeout_seconds": self.api_connect_timeout_seconds,
            "api_read_timeout_seconds": self.api_read_timeout_seconds,
            "api_write_timeout_seconds": self.api_write_timeout_seconds,
            "api_pool_timeout_seconds": self.api_pool_timeout_seconds,
            "api_max_retry_delay_seconds": self.api_max_retry_delay_seconds,
            "raw_data_dir": str(self.raw_data_dir),
            "pg_dsn": "***REDACTED***" if self.pg_dsn else None,
            "raw_schema": self.raw_schema,
            "ops_schema": self.ops_schema,
            "staging_schema": self.staging_schema,
            "input_layer_schema": self.input_layer_schema,
            "analytics_core_schema": self.analytics_core_schema,
            "analytics_marts_schema": self.analytics_marts_schema,
            "object_storage_provider": self.object_storage_provider,
            "object_storage_bucket": self.object_storage_bucket,
            "object_storage_prefix": self.object_storage_prefix,
            "object_storage_region": self.object_storage_region,
            "object_storage_endpoint_url": self.object_storage_endpoint_url,
            "object_storage_local_root": str(self.object_storage_local_root),
            "dbt_target": self.dbt_target,
            "dbt_profiles_dir": str(self.dbt_profiles_dir),
            "dbt_project_dir": str(self.dbt_project_dir),
            "pipeline_environment": self.pipeline_environment,
            "pipeline_max_success_age_hours": self.pipeline_max_success_age_hours,
            "log_level": self.log_level,
            "source_name": self.source_name,
            "ingest_role": self.ingest_role,
            "transform_role": self.transform_role,
            "api_secret_provider": self.api_secret_provider,
            "api_secret_id": self.api_secret_id,
            "aws_region": self.aws_region,
            "api_page_size": self.api_page_size,
            "api_max_pages": self.api_max_pages,
            "api_max_page_bytes": self.api_max_page_bytes,
        }

    def __repr__(self) -> str:  # never let a stray print() leak secrets
        return f"IngestConfig({self.safe_dict()!r})"

    def __str__(self) -> str:  # match __repr__ -- never the pydantic default
        return self.__repr__()
