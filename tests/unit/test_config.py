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
        self.assertEqual(config.raw_schema, "raw")
        self.assertEqual(config.ops_schema, "ingest_ops")
        self.assertEqual(config.input_layer_schema, "input_layer")
        self.assertEqual(config.source_name, "tuva")
        self.assertEqual(config.ingest_role, "tuva_ingest_role")
        self.assertEqual(config.transform_role, "tuva_transform_role")

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
