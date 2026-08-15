"""Structural checks on Dockerfile/.dockerignore/compose.yaml that don't
require a Docker daemon (unavailable in this sandbox -- see
docs/RUNBOOK.md / the final validation report for what still needs a real
`docker build`). These catch the concrete regressions a reviewer could
introduce by hand: a stray root user, an embedded secret, a missing
HEALTHCHECK, or `shell=True`-style entrypoint that swallows SIGTERM.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import yaml  # noqa: F401

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


class TestDockerfile(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_multi_stage_build(self):
        self.assertIn("FROM python", self.text)
        self.assertIn(" AS builder", self.text)
        self.assertIn(" AS runtime", self.text)

    def test_pinned_python_minor_version(self):
        self.assertRegex(self.text, r"ARG PYTHON_VERSION=3\.12\.\d+")

    def test_locked_install_only(self):
        self.assertIn("uv sync --locked", self.text)
        self.assertNotIn("pip install", self.text)

    def test_psql_client_installed(self):
        self.assertIn("postgresql-client", self.text)

    def test_runs_as_non_root_user(self):
        self.assertRegex(self.text, r"useradd .*--uid 10001")
        self.assertIn("USER tuva", self.text)
        # USER must come after the useradd/chown steps, not before
        user_idx = self.text.index("USER tuva")
        useradd_idx = self.text.index("useradd")
        self.assertLess(useradd_idx, user_idx)

    def test_healthcheck_uses_app_health_command(self):
        self.assertIn("HEALTHCHECK", self.text)
        self.assertIn('CMD ["tuva-postgres", "healthcheck"]', self.text)

    def test_explicit_entrypoint_exec_form(self):
        self.assertIn('ENTRYPOINT ["tuva-postgres"]', self.text)

    def test_no_embedded_credentials_or_env_file(self):
        # a usage comment may *mention* --env-file .env; the image must
        # never COPY/ADD one in, or hardcode a secret-shaped value.
        self.assertNotIn("COPY .env", self.text)
        self.assertNotIn("ADD .env", self.text)
        lowered = self.text.lower()
        for bad in ("password=", "api_token=", "pg_dsn=postgresql://"):
            self.assertNotIn(bad, lowered)

    def test_declares_oci_labels(self):
        self.assertIn("org.opencontainers.image.title", self.text)

    def test_only_declared_writable_locations(self):
        self.assertIn("RAW_DATA_DIR=/app/data/raw", self.text)
        self.assertIn("mkdir -p /app/data/raw /app/data/metrics /app/tmp", self.text)


class TestDockerignore(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")

    def test_excludes_git_and_env_and_data(self):
        for pattern in (".git/", ".env", "data/", "tmp/", ".venv/", "tests/"):
            self.assertIn(pattern, self.text)

    def test_excludes_report_pdf_and_caches(self):
        self.assertIn("__pycache__/", self.text)
        self.assertIn("report.pdf", self.text)


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestComposeYaml(unittest.TestCase):
    def setUp(self):
        self.raw_text = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.doc = yaml.safe_load(self.raw_text)

    def test_has_postgres_and_pipeline_services(self):
        self.assertIn("postgres", self.doc["services"])
        self.assertIn("pipeline", self.doc["services"])

    def test_postgres_has_healthcheck(self):
        self.assertIn("healthcheck", self.doc["services"]["postgres"])

    def test_pipeline_depends_on_healthy_postgres(self):
        dep = self.doc["services"]["pipeline"]["depends_on"]["postgres"]
        self.assertEqual(dep["condition"], "service_healthy")

    def test_raw_data_volume_is_persistent_named_volume(self):
        pipeline_volumes = self.doc["services"]["pipeline"]["volumes"]
        self.assertTrue(any("raw-data:/app/data/raw" in v for v in pipeline_volumes))
        self.assertIn("raw-data", self.doc["volumes"])

    def test_credentials_are_clearly_labeled_local_only_example(self):
        # the local-only password must be present (so compose actually
        # works out of the box) but the file must call out, in nearby
        # comments, that it is not a production credential.
        self.assertIn("local-only-example-password-change-me", self.raw_text)
        self.assertIn("LOCAL-ONLY EXAMPLE CREDENTIALS", self.raw_text)

    def test_default_command_does_not_require_api_credentials(self):
        # `docker compose up` must not fail out of the box just because
        # TUVA_API_MANIFEST_URL/TOKEN aren't set.
        self.assertEqual(self.doc["services"]["pipeline"]["command"], ["healthcheck"])
        env = self.doc["services"]["pipeline"]["environment"]
        self.assertNotIn("TUVA_API_TOKEN", env)


if __name__ == "__main__":
    unittest.main()
