"""Standard-library unit tests for tuva_ingest.config.IngestConfig."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.config import ALL_REQUIREMENTS, REQUIRE_DB, IngestConfig  # noqa: E402
from pydantic import SecretStr  # noqa: E402
from tuva_ingest.errors import ConfigError  # noqa: E402

_ENV_KEYS = [
    "TUVA_API_MANIFEST_URL",
    "TUVA_API_TOKEN",
    "TUVA_API_TIMEOUT_SECONDS",
    "TUVA_API_MAX_RETRIES",
    "TUVA_API_ALLOW_INSECURE_HTTP",
    "RAW_DATA_DIR",
    "PG_DSN",
    "RAW_SCHEMA",
    "OPS_SCHEMA",
    "INPUT_LAYER_SCHEMA",
    "DBT_TARGET",
    "DBT_PROFILES_DIR",
    "DBT_PROJECT_DIR",
    "PIPELINE_ENVIRONMENT",
    "PIPELINE_MAX_SUCCESS_AGE_HOURS",
    "LOG_LEVEL",
    "SOURCE_NAME",
    "INGEST_ROLE",
    "TRANSFORM_ROLE",
    "TUVA_API_CONNECT_TIMEOUT_SECONDS",
    "TUVA_API_READ_TIMEOUT_SECONDS",
    "TUVA_API_WRITE_TIMEOUT_SECONDS",
    "TUVA_API_POOL_TIMEOUT_SECONDS",
    "TUVA_API_MAX_RETRY_DELAY_SECONDS",
    "TUVA_API_SECRET_PROVIDER",
    "TUVA_API_SECRET_ID",
    "AWS_REGION",
    "TUVA_API_PAGE_SIZE",
    "TUVA_API_MAX_PAGES",
    "TUVA_API_MAX_PAGE_BYTES",
    "TUVA_API_MAX_RECORDS_PER_RUN",
    "TUVA_API_MAX_RETRY_DURATION_SECONDS",
    "TUVA_OAUTH_TOKEN_URL",
    "TUVA_OAUTH_CLIENT_ID",
    "TUVA_OAUTH_CLIENT_SECRET",
    "TUVA_OAUTH_SCOPES",
    "TUVA_OAUTH_REFRESH_SKEW_SECONDS",
]


def _valid_env():
    return {
        "TUVA_API_MANIFEST_URL": "https://example.invalid/manifest.json",
        "TUVA_API_TOKEN": "test-token",
        "PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "RAW_DATA_DIR": "data/raw",
    }


class _EnvIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set(self, **kwargs):
        for k, v in kwargs.items():
            os.environ[k] = v


class TestIngestConfig(_EnvIsolatedTestCase):
    def test_valid_config_loads_with_defaults(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        # Six-schema lineage defaults (see docs/SOURCE_CONTRACT.md "Schema
        # lineage") -- changed from raw/ingest_ops as part of adding
        # object storage; RAW_SCHEMA/OPS_SCHEMA overrides still work
        # exactly as before (see test_unsafe_raw_schema_identifier_rejected
        # and the override tests below).
        self.assertEqual(config.raw_schema, "raw_incoming")
        self.assertEqual(config.ops_schema, "ops")
        self.assertEqual(config.staging_schema, "staging_incoming")
        self.assertEqual(config.input_layer_schema, "input_layer")
        self.assertEqual(config.analytics_core_schema, "analytics_core")
        self.assertEqual(config.analytics_marts_schema, "analytics_marts")
        self.assertEqual(config.source_name, "tuva")
        self.assertEqual(config.ingest_role, "tuva_ingest_role")
        self.assertEqual(config.transform_role, "tuva_transform_role")
        # Object storage defaults (see docs/SOURCE_CONTRACT.md "Object storage").
        self.assertEqual(config.object_storage_provider, "local")
        self.assertIsNone(config.object_storage_bucket)
        self.assertEqual(config.object_storage_prefix, "raw")
        self.assertIsNone(config.object_storage_region)
        self.assertIsNone(config.object_storage_endpoint_url)

    def test_raw_schema_override_still_works_for_existing_deployments(self):
        # An existing deployment that explicitly pins RAW_SCHEMA/OPS_SCHEMA
        # to its pre-existing value must keep working unchanged -- only the
        # DEFAULT changed, never override behavior.
        env = _valid_env()
        env["RAW_SCHEMA"] = "raw"
        env["OPS_SCHEMA"] = "ingest_ops"
        self._set(**env)
        config = IngestConfig.load()
        self.assertEqual(config.raw_schema, "raw")
        self.assertEqual(config.ops_schema, "ingest_ops")

    def test_object_storage_s3_requires_bucket(self):
        env = _valid_env()
        env["OBJECT_STORAGE_PROVIDER"] = "s3"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("OBJECT_STORAGE_BUCKET", str(ctx.exception))

    def test_object_storage_s3_with_bucket_succeeds(self):
        env = _valid_env()
        env["OBJECT_STORAGE_PROVIDER"] = "s3"
        env["OBJECT_STORAGE_BUCKET"] = "my-bucket"
        self._set(**env)
        config = IngestConfig.load()
        self.assertEqual(config.object_storage_bucket, "my-bucket")

    def test_unsupported_object_storage_provider_rejected(self):
        env = _valid_env()
        env["OBJECT_STORAGE_PROVIDER"] = "azure-blob"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_object_storage_config_redacted_in_safe_dict(self):
        # No credentials are ever stored in IngestConfig for object
        # storage (ambient credentials only) -- safe_dict() should
        # surface the non-secret settings plainly (nothing to redact).
        env = _valid_env()
        env["OBJECT_STORAGE_BUCKET"] = "my-bucket"
        self._set(**env)
        config = IngestConfig.load()
        safe = config.safe_dict()
        self.assertEqual(safe["object_storage_bucket"], "my-bucket")
        self.assertNotIn("access_key", str(safe).lower())
        self.assertNotIn("secret_key", str(safe).lower())

    def test_six_schemas_must_be_pairwise_distinct(self):
        env = _valid_env()
        env["ANALYTICS_CORE_SCHEMA"] = "analytics_marts"  # collides with the other default
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_missing_required_fields_fail_fast_with_all_errors_listed(self):
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load(required=ALL_REQUIREMENTS)
        message = str(ctx.exception)
        self.assertIn("TUVA_API_MANIFEST_URL", message)
        self.assertIn("TUVA_API_TOKEN", message)
        self.assertIn("PG_DSN", message)

    def test_db_only_requirement_does_not_need_api_token(self):
        self._set(PG_DSN="postgresql://user:pass@localhost:5432/db")
        config = IngestConfig.load(required=REQUIRE_DB)
        self.assertIsNone(config.api_token)

    def test_https_required_by_default(self):
        env = _valid_env()
        env["TUVA_API_MANIFEST_URL"] = "http://example.invalid/manifest.json"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("HTTPS", str(ctx.exception))

    def test_http_allowed_when_insecure_enabled(self):
        env = _valid_env()
        env["TUVA_API_MANIFEST_URL"] = "http://example.invalid/manifest.json"
        env["TUVA_API_ALLOW_INSECURE_HTTP"] = "1"
        self._set(**env)
        config = IngestConfig.load()
        self.assertTrue(config.api_allow_insecure_http)

    def test_invalid_timeout_rejected(self):
        env = _valid_env()
        env["TUVA_API_TIMEOUT_SECONDS"] = "not-a-number"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("TUVA_API_TIMEOUT_SECONDS", str(ctx.exception))

    def test_negative_timeout_rejected(self):
        env = _valid_env()
        env["TUVA_API_TIMEOUT_SECONDS"] = "-1"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_negative_max_retries_rejected(self):
        env = _valid_env()
        env["TUVA_API_MAX_RETRIES"] = "-1"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_unsafe_raw_schema_identifier_rejected(self):
        env = _valid_env()
        env["RAW_SCHEMA"] = "raw; DROP TABLE x"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_ops_schema_must_differ_from_raw_schema(self):
        env = _valid_env()
        env["OPS_SCHEMA"] = "raw"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("OPS_SCHEMA must differ from RAW_SCHEMA", str(ctx.exception))

    def test_input_layer_schema_must_differ_from_raw_schema(self):
        env = _valid_env()
        env["INPUT_LAYER_SCHEMA"] = "raw"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("INPUT_LAYER_SCHEMA must differ from RAW_SCHEMA", str(ctx.exception))

    def test_ingest_role_must_differ_from_transform_role(self):
        env = _valid_env()
        env["INGEST_ROLE"] = "same_role"
        env["TRANSFORM_ROLE"] = "same_role"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("INGEST_ROLE and TRANSFORM_ROLE must differ", str(ctx.exception))

    def test_unsafe_role_identifier_rejected(self):
        env = _valid_env()
        env["INGEST_ROLE"] = "bad role"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("INGEST_ROLE", str(ctx.exception))

    def test_invalid_log_level_rejected(self):
        env = _valid_env()
        env["LOG_LEVEL"] = "VERY_LOUD"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_empty_source_name_rejected(self):
        env = _valid_env()
        env["SOURCE_NAME"] = "   "
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_unsafe_raw_data_dir_rejected(self):
        env = _valid_env()
        env["RAW_DATA_DIR"] = "/"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_dbt_settings_passed_through(self):
        env = _valid_env()
        env["DBT_TARGET"] = "ci"
        env["DBT_PROFILES_DIR"] = "/app"
        env["DBT_PROJECT_DIR"] = "/app"
        self._set(**env)
        config = IngestConfig.load()
        self.assertEqual(config.dbt_target, "ci")
        self.assertEqual(str(config.dbt_profiles_dir), "/app")
        self.assertEqual(str(config.dbt_project_dir), "/app")

    def test_safe_dict_redacts_secrets(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        safe = config.safe_dict()
        self.assertEqual(safe["api_token"], "***REDACTED***")
        self.assertEqual(safe["pg_dsn"], "***REDACTED***")
        self.assertNotIn("test-token", str(safe))
        self.assertNotIn("user:pass", str(safe))

    def test_repr_never_leaks_secrets(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertNotIn("test-token", repr(config))
        self.assertNotIn("user:pass", repr(config))


class TestIngestConfigPydanticTypes(_EnvIsolatedTestCase):
    """Config is now a pydantic-settings BaseSettings model: credentials
    must be SecretStr (never a bare str), timeout fields must be
    positive floats or None, and httpx_timeout() must build a real
    httpx.Timeout using per-phase overrides when present."""

    def test_api_token_is_a_secret_str(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertIsInstance(config.api_token, SecretStr)
        self.assertNotIn("test-token", str(config.api_token))
        self.assertEqual(config.api_token_value, "test-token")

    def test_pg_dsn_is_a_secret_str(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertIsInstance(config.pg_dsn, SecretStr)
        self.assertNotIn("user:pass", str(config.pg_dsn))
        self.assertEqual(config.pg_dsn_value, "postgresql://user:pass@localhost:5432/db")

    def test_config_is_frozen(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        with self.assertRaises(Exception):
            config.source_name = "other"  # type: ignore[misc]

    def test_per_phase_timeout_overrides_used_when_set(self):
        env = _valid_env()
        env["TUVA_API_CONNECT_TIMEOUT_SECONDS"] = "2.5"
        env["TUVA_API_READ_TIMEOUT_SECONDS"] = "9.5"
        env["TUVA_API_WRITE_TIMEOUT_SECONDS"] = "9.5"
        env["TUVA_API_POOL_TIMEOUT_SECONDS"] = "1.0"
        self._set(**env)
        config = IngestConfig.load()
        timeout = config.httpx_timeout()
        self.assertEqual(timeout.connect, 2.5)
        self.assertEqual(timeout.read, 9.5)
        self.assertEqual(timeout.write, 9.5)
        self.assertEqual(timeout.pool, 1.0)

    def test_timeout_falls_back_to_api_timeout_seconds_when_unset(self):
        env = _valid_env()
        env["TUVA_API_TIMEOUT_SECONDS"] = "12"
        self._set(**env)
        config = IngestConfig.load()
        timeout = config.httpx_timeout()
        self.assertEqual(timeout.connect, 12.0)
        self.assertEqual(timeout.read, 12.0)

    def test_negative_connect_timeout_rejected(self):
        env = _valid_env()
        env["TUVA_API_CONNECT_TIMEOUT_SECONDS"] = "-1"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_negative_max_retry_delay_rejected(self):
        env = _valid_env()
        env["TUVA_API_MAX_RETRY_DELAY_SECONDS"] = "0"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_max_retry_delay_defaults_to_30(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_max_retry_delay_seconds, 30.0)


class TestIngestConfigSecretProviderAndPagination(_EnvIsolatedTestCase):
    """TUVA_API_SECRET_PROVIDER/TUVA_API_SECRET_ID/AWS_REGION (secrets.py)
    and the paginated-extraction tuning fields (pagination.py)."""

    def test_default_secret_provider_is_env(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_secret_provider, "env")
        self.assertIsNone(config.api_secret_id)

    def test_aws_provider_accepted_with_secret_id(self):
        env = _valid_env()
        env["TUVA_API_SECRET_PROVIDER"] = "aws"
        env["TUVA_API_SECRET_ID"] = "prod/tuva/api-token"
        env["AWS_REGION"] = "us-east-1"
        self._set(**env)
        config = IngestConfig.load()
        self.assertEqual(config.api_secret_provider, "aws")
        self.assertEqual(config.api_secret_id, "prod/tuva/api-token")
        self.assertEqual(config.aws_region, "us-east-1")

    def test_aws_provider_without_secret_id_rejected(self):
        env = _valid_env()
        env["TUVA_API_SECRET_PROVIDER"] = "aws"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("TUVA_API_SECRET_ID", str(ctx.exception))

    def test_unknown_secret_provider_rejected(self):
        env = _valid_env()
        env["TUVA_API_SECRET_PROVIDER"] = "vault"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_page_size_defaults_to_none(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertIsNone(config.api_page_size)

    def test_page_size_must_be_positive(self):
        env = _valid_env()
        env["TUVA_API_PAGE_SIZE"] = "0"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_max_pages_defaults_to_10000(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_max_pages, 10_000)

    def test_max_pages_must_be_positive(self):
        env = _valid_env()
        env["TUVA_API_MAX_PAGES"] = "-1"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_max_page_bytes_has_a_sensible_default(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_max_page_bytes, 64 * 1024 * 1024)

    def test_secret_id_never_redacted_in_safe_dict_it_is_non_secret_lookup_info(self):
        env = _valid_env()
        env["TUVA_API_SECRET_PROVIDER"] = "aws"
        env["TUVA_API_SECRET_ID"] = "prod/tuva/api-token"
        self._set(**env)
        config = IngestConfig.load()
        safe = config.safe_dict()
        self.assertEqual(safe["api_secret_id"], "prod/tuva/api-token")

    def test_max_records_per_run_has_a_sensible_default(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_max_records_per_run, 2_000_000)

    def test_max_records_per_run_must_be_positive(self):
        env = _valid_env()
        env["TUVA_API_MAX_RECORDS_PER_RUN"] = "0"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_max_retry_duration_seconds_has_a_sensible_default(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertEqual(config.api_max_retry_duration_seconds, 120.0)

    def test_max_retry_duration_seconds_must_be_positive(self):
        env = _valid_env()
        env["TUVA_API_MAX_RETRY_DURATION_SECONDS"] = "-5"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_max_retry_duration_seconds_rejects_malformed_value(self):
        env = _valid_env()
        env["TUVA_API_MAX_RETRY_DURATION_SECONDS"] = "not-a-number"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()


class TestIngestConfigOAuth(_EnvIsolatedTestCase):
    """TUVA_OAUTH_TOKEN_URL/_CLIENT_ID/_CLIENT_SECRET/_SCOPES/_REFRESH_SKEW_SECONDS
    (oauth.py). OAuth is entirely optional -- unset by default, in which
    case this connector's pre-existing static-bearer-token mode applies
    unchanged."""

    def test_oauth_fields_default_to_unset(self):
        self._set(**_valid_env())
        config = IngestConfig.load()
        self.assertIsNone(config.oauth_token_url)
        self.assertIsNone(config.oauth_client_id)
        self.assertIsNone(config.oauth_client_secret)
        self.assertEqual(config.oauth_refresh_skew_seconds, 60.0)

    def test_oauth_token_url_alone_without_client_id_is_rejected(self):
        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "https://example.invalid/oauth/token"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("TUVA_OAUTH_CLIENT_ID", str(ctx.exception))

    def test_oauth_token_url_alone_without_client_secret_is_rejected(self):
        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "https://example.invalid/oauth/token"
        env["TUVA_OAUTH_CLIENT_ID"] = "client-1"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        self.assertIn("TUVA_OAUTH_CLIENT_SECRET", str(ctx.exception))

    def test_fully_configured_oauth_is_accepted(self):
        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "https://example.invalid/oauth/token"
        env["TUVA_OAUTH_CLIENT_ID"] = "client-1"
        env["TUVA_OAUTH_CLIENT_SECRET"] = "super-secret-value"
        env["TUVA_OAUTH_SCOPES"] = "read write"
        self._set(**env)
        config = IngestConfig.load()
        self.assertEqual(config.oauth_token_url, "https://example.invalid/oauth/token")
        self.assertEqual(config.oauth_client_id, "client-1")
        self.assertEqual(config.oauth_client_secret_value, "super-secret-value")
        self.assertEqual(config.oauth_scopes, "read write")

    def test_oauth_client_secret_redacted_in_safe_dict(self):
        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "https://example.invalid/oauth/token"
        env["TUVA_OAUTH_CLIENT_ID"] = "client-1"
        env["TUVA_OAUTH_CLIENT_SECRET"] = "super-secret-value"
        self._set(**env)
        config = IngestConfig.load()
        safe = config.safe_dict()
        self.assertEqual(safe["oauth_client_secret"], "***REDACTED***")
        self.assertNotIn("super-secret-value", repr(config))
        self.assertNotIn("super-secret-value", str(config))

    def test_oauth_client_secret_is_a_secret_str_type(self):
        from pydantic import SecretStr as _SecretStr

        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "https://example.invalid/oauth/token"
        env["TUVA_OAUTH_CLIENT_ID"] = "client-1"
        env["TUVA_OAUTH_CLIENT_SECRET"] = "super-secret-value"
        self._set(**env)
        config = IngestConfig.load()
        self.assertIsInstance(config.oauth_client_secret, _SecretStr)

    def test_plain_http_oauth_token_url_rejected_by_default(self):
        env = _valid_env()
        env["TUVA_OAUTH_TOKEN_URL"] = "http://example.invalid/oauth/token"
        env["TUVA_OAUTH_CLIENT_ID"] = "client-1"
        env["TUVA_OAUTH_CLIENT_SECRET"] = "secret"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()

    def test_refresh_skew_seconds_must_be_positive(self):
        env = _valid_env()
        env["TUVA_OAUTH_REFRESH_SKEW_SECONDS"] = "0"
        self._set(**env)
        with self.assertRaises(ConfigError):
            IngestConfig.load()


_HOSTILE_IDENTIFIERS = [
    "",
    "raw; DROP TABLE eligibility",
    "raw--comment",
    "raw ops",
    "raw.ops",
    'raw"ops',
    "raw'ops",
    "raw\nops",
    "1raw",
]


class TestIngestConfigHostileSchemaInputs(_EnvIsolatedTestCase):
    """Defense-in-depth: config.py must reject hostile RAW_SCHEMA/
    OPS_SCHEMA/INPUT_LAYER_SCHEMA values through the same shared
    identifier policy that db.py's low-level SQL composition uses again
    independently -- proving both layers are wired to the real policy."""

    def test_hostile_raw_schema_rejected(self):
        for hostile in _HOSTILE_IDENTIFIERS:
            with self.subTest(raw_schema=hostile):
                env = _valid_env()
                env["RAW_SCHEMA"] = hostile
                self._set(**env)
                with self.assertRaises(ConfigError) as ctx:
                    IngestConfig.load()
                self.assertIn("RAW_SCHEMA", str(ctx.exception))
                self._restore()
                for k in _ENV_KEYS:
                    os.environ.pop(k, None)

    def test_hostile_ops_schema_rejected(self):
        for hostile in _HOSTILE_IDENTIFIERS:
            with self.subTest(ops_schema=hostile):
                env = _valid_env()
                env["OPS_SCHEMA"] = hostile
                self._set(**env)
                with self.assertRaises(ConfigError) as ctx:
                    IngestConfig.load()
                self.assertIn("OPS_SCHEMA", str(ctx.exception))
                self._restore()
                for k in _ENV_KEYS:
                    os.environ.pop(k, None)

    def test_hostile_schema_error_never_includes_secrets(self):
        env = _valid_env()
        env["RAW_SCHEMA"] = "raw; DROP TABLE x"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            IngestConfig.load()
        message = str(ctx.exception)
        self.assertNotIn("test-token", message)
        self.assertNotIn("user:pass", message)


if __name__ == "__main__":
    unittest.main()
