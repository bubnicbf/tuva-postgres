"""Standard-library unit tests for tuva_postgres.config.PipelineConfig."""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.config import ALL_REQUIREMENTS, REQUIRE_DB, PipelineConfig  # noqa: E402
from tuva_postgres.errors import ConfigError  # noqa: E402

_ENV_KEYS = [
    "TUVA_API_MANIFEST_URL",
    "TUVA_API_TOKEN",
    "TUVA_API_TIMEOUT_SECONDS",
    "TUVA_API_MAX_RETRIES",
    "TUVA_API_ALLOW_INSECURE_HTTP",
    "RAW_DATA_DIR",
    "PG_DSN",
    "PG_SCHEMA",
    "TERMINOLOGY_SCHEMA",
    "OPS_SCHEMA",
    "PIPELINE_ENVIRONMENT",
    "PIPELINE_MAX_SUCCESS_AGE_HOURS",
    "LOG_LEVEL",
    "METRICS_FILE",
]


def _valid_env():
    return {
        "TUVA_API_MANIFEST_URL": "https://example.invalid/manifest.json",
        "TUVA_API_TOKEN": "test-token",
        "PG_DSN": "postgresql://user:pass@localhost:5432/db",
        "RAW_DATA_DIR": "data/raw",
    }


class TestPipelineConfig(unittest.TestCase):
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

    def test_valid_config_loads(self):
        self._set(**_valid_env())
        config = PipelineConfig.load()
        self.assertEqual(config.pg_schema, "tuva")
        self.assertEqual(config.terminology_schema, "tuva_term")
        self.assertEqual(config.ops_schema, "tuva_ops")

    def test_missing_required_fields_fail_fast_with_all_errors_listed(self):
        with self.assertRaises(ConfigError) as ctx:
            PipelineConfig.load(required=ALL_REQUIREMENTS)
        message = str(ctx.exception)
        self.assertIn("TUVA_API_MANIFEST_URL", message)
        self.assertIn("TUVA_API_TOKEN", message)
        self.assertIn("PG_DSN", message)

    def test_db_only_requirement_does_not_need_api_token(self):
        self._set(PG_DSN="postgresql://user:pass@localhost:5432/db")
        config = PipelineConfig.load(required=REQUIRE_DB)
        self.assertIsNone(config.api_token)

    def test_https_required_by_default(self):
        env = _valid_env()
        env["TUVA_API_MANIFEST_URL"] = "http://example.invalid/manifest.json"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            PipelineConfig.load()
        self.assertIn("HTTPS", str(ctx.exception))

    def test_http_allowed_when_insecure_enabled(self):
        env = _valid_env()
        env["TUVA_API_MANIFEST_URL"] = "http://example.invalid/manifest.json"
        env["TUVA_API_ALLOW_INSECURE_HTTP"] = "1"
        self._set(**env)
        config = PipelineConfig.load()
        self.assertTrue(config.api_allow_insecure_http)

    def test_unsafe_schema_identifier_rejected(self):
        env = _valid_env()
        env["PG_SCHEMA"] = "tuva; DROP TABLE x"
        self._set(**env)
        with self.assertRaises(ConfigError):
            PipelineConfig.load()

    def test_ops_schema_must_differ_from_pg_schema(self):
        env = _valid_env()
        env["OPS_SCHEMA"] = "tuva"
        self._set(**env)
        with self.assertRaises(ConfigError):
            PipelineConfig.load()

    def test_invalid_log_level_rejected(self):
        env = _valid_env()
        env["LOG_LEVEL"] = "VERY_LOUD"
        self._set(**env)
        with self.assertRaises(ConfigError):
            PipelineConfig.load()

    def test_safe_dict_redacts_secrets(self):
        self._set(**_valid_env())
        config = PipelineConfig.load()
        safe = config.safe_dict()
        self.assertEqual(safe["api_token"], "***REDACTED***")
        self.assertEqual(safe["pg_dsn"], "***REDACTED***")
        self.assertNotIn("test-token", str(safe))
        self.assertNotIn("user:pass", str(safe))

    def test_repr_never_leaks_secrets(self):
        self._set(**_valid_env())
        config = PipelineConfig.load()
        self.assertNotIn("test-token", repr(config))
        self.assertNotIn("user:pass", repr(config))


_HOSTILE_IDENTIFIERS = [
    "",
    "tuva; DROP TABLE patient",
    "tuva--comment",
    "tuva ops",
    "tuva.ops",
    'tuva"ops',
    "tuva'ops",
    "tuva\nops",
    "1tuva",
    # Note: a literal null byte is intentionally excluded here -- the
    # OS environment itself (os.environ / putenv) cannot hold one, so it
    # can never reach config.py via an env var in the first place. Null
    # byte rejection by the shared policy itself is covered directly in
    # tests/unit/test_identifiers.py.
]


class TestPipelineConfigHostileSchemaInputs(unittest.TestCase):
    """Defense-in-depth: config.py must reject hostile PG_SCHEMA /
    TERMINOLOGY_SCHEMA / OPS_SCHEMA values through the same shared
    identifier policy that db.py's low-level SQL composition uses again
    independently -- proving both layers are wired to the real policy,
    not a looser or divergent local copy."""

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

    def test_hostile_pg_schema_rejected(self):
        for hostile in _HOSTILE_IDENTIFIERS:
            with self.subTest(pg_schema=hostile):
                env = _valid_env()
                env["PG_SCHEMA"] = hostile
                env["TERMINOLOGY_SCHEMA"] = "tuva_term"  # keep this field valid in isolation
                self._set(**env)
                with self.assertRaises(ConfigError) as ctx:
                    PipelineConfig.load()
                self.assertIn("PG_SCHEMA", str(ctx.exception))
                self._restore()
                for k in _ENV_KEYS:
                    os.environ.pop(k, None)

    def test_hostile_terminology_schema_rejected(self):
        for hostile in _HOSTILE_IDENTIFIERS:
            with self.subTest(terminology_schema=hostile):
                env = _valid_env()
                env["TERMINOLOGY_SCHEMA"] = hostile
                self._set(**env)
                with self.assertRaises(ConfigError) as ctx:
                    PipelineConfig.load()
                self.assertIn("TERMINOLOGY_SCHEMA", str(ctx.exception))
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
                    PipelineConfig.load()
                self.assertIn("OPS_SCHEMA", str(ctx.exception))
                self._restore()
                for k in _ENV_KEYS:
                    os.environ.pop(k, None)

    def test_all_three_hostile_schemas_reported_together(self):
        # Fail-fast-with-everything-listed behavior must still hold when
        # the newly-shared validator is the one raising each error.
        env = _valid_env()
        env["PG_SCHEMA"] = "tuva; DROP TABLE x"
        env["TERMINOLOGY_SCHEMA"] = "bad term"
        env["OPS_SCHEMA"] = "bad ops"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            PipelineConfig.load()
        message = str(ctx.exception)
        self.assertIn("PG_SCHEMA", message)
        self.assertIn("TERMINOLOGY_SCHEMA", message)
        self.assertIn("OPS_SCHEMA", message)

    def test_hostile_schema_error_never_includes_secrets(self):
        env = _valid_env()
        env["PG_SCHEMA"] = "tuva; DROP TABLE x"
        self._set(**env)
        with self.assertRaises(ConfigError) as ctx:
            PipelineConfig.load()
        message = str(ctx.exception)
        self.assertNotIn("test-token", message)
        self.assertNotIn("user:pass", message)


if __name__ == "__main__":
    unittest.main()
