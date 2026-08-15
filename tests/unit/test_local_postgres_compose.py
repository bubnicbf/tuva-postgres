"""Database-free structural tests for the local Docker Compose PostgreSQL
development environment (compose.yaml, the Makefile's `local-db-*`
targets, and the checked-in environment examples). No Docker daemon is
required or used anywhere in this module -- see
scripts/tests/test_local_postgres_compose.sh for the isolated runtime
smoke test that actually starts containers.

Deliberately complements, not duplicates, tests/unit/test_container_
structure.py's TestComposeYaml (which covers the original, smaller set
of compose.yaml assertions from before this local-Postgres workflow
existed). This module owns the newer, broader contract: execution
modes/services layout, Makefile lifecycle-target semantics, and
cross-file consistency between compose.yaml and the checked-in
environment examples/README.
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import yaml

    HAVE_YAML = True
except ImportError:
    HAVE_YAML = False


def _parse_port_mapping(mapping: str) -> tuple[str, str, str]:
    """Split a compose port-mapping string like
    '127.0.0.1:${POSTGRES_PORT:-5432}:5432' into (host_ip, host_port_expr,
    container_port). A plain str.split(':') breaks here because the
    interpolation expression itself contains a colon."""
    match = re.match(r"^([\d.]+):(\$\{[^}]+\}|[\w.-]+):(\d+)$", mapping)
    assert match is not None, f"unrecognized port mapping format: {mapping!r}"
    return match.group(1), match.group(2), match.group(3)


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestLocalPostgresComposeStructure(unittest.TestCase):
    def setUp(self):
        self.raw_text = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.doc = yaml.safe_load(self.raw_text)
        self.services = self.doc["services"]
        self.postgres = self.services["postgres"]

    # --- PostgreSQL service ------------------------------------------------
    def test_postgres_service_exists(self):
        self.assertIn("postgres", self.services)

    def test_image_is_pinned_not_latest(self):
        image = self.postgres["image"]
        self.assertNotEqual(image.split(":")[-1], "latest")
        self.assertIn(":", image, "image should carry an explicit tag")

    def test_database_user_and_local_password_defined(self):
        env = self.postgres["environment"]
        for key in ("POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB"):
            self.assertIn(key, env)
            self.assertTrue(str(env[key]).strip(), f"{key} must not be empty")

    def test_published_port_is_loopback_bound(self):
        ports = self.postgres["ports"]
        self.assertEqual(len(ports), 1)
        host_ip, _host_port_expr, _container_port = _parse_port_mapping(ports[0])
        self.assertEqual(host_ip, "127.0.0.1")
        self.assertNotIn("0.0.0.0", ports[0])

    def test_host_port_is_configurable(self):
        ports = self.postgres["ports"]
        _host_ip, host_port_expr, _container_port = _parse_port_mapping(ports[0])
        self.assertTrue(host_port_expr.startswith("${") and host_port_expr.endswith("}"))
        self.assertIn("POSTGRES_PORT", host_port_expr)

    def test_container_port_remains_5432(self):
        ports = self.postgres["ports"]
        _host_ip, _host_port_expr, container_port = _parse_port_mapping(ports[0])
        self.assertEqual(container_port, "5432")

    def test_named_data_volume_mounted_at_correct_path(self):
        volumes = self.postgres["volumes"]
        matches = [v for v in volumes if v.endswith(":/var/lib/postgresql/data")]
        self.assertEqual(len(matches), 1, "expected exactly one /var/lib/postgresql/data mount")
        volume_name = matches[0].split(":")[0]
        self.assertIn(volume_name, self.doc["volumes"], f"{volume_name!r} must be a declared named volume")

    def test_postgres_has_valid_healthcheck(self):
        healthcheck = self.postgres.get("healthcheck")
        self.assertIsNotNone(healthcheck)
        test_cmd = " ".join(healthcheck["test"])
        self.assertIn("pg_isready", test_cmd)
        for field in ("interval", "timeout", "retries"):
            self.assertIn(field, healthcheck)

    def test_healthcheck_references_configured_local_user_and_database(self):
        # Derived from the same environment block, not hardcoded, so this
        # stays correct if the local credentials/database name ever change.
        env = self.postgres["environment"]
        healthcheck_cmd = " ".join(self.postgres["healthcheck"]["test"])
        self.assertIn(env["POSTGRES_USER"], healthcheck_cmd)
        self.assertIn(env["POSTGRES_DB"], healthcheck_cmd)

    # --- migration / pipeline services --------------------------------------
    def test_one_shot_migration_service_exists(self):
        self.assertIn("migrate", self.services)
        migrate = self.services["migrate"]
        self.assertEqual(migrate.get("restart", "no"), "no")

    def test_migration_service_waits_for_healthy_postgres(self):
        dep = self.services["migrate"]["depends_on"]["postgres"]
        self.assertEqual(dep["condition"], "service_healthy")

    def test_pipeline_waits_for_migration_to_complete(self):
        # Uses the Compose v2 service_completed_successfully condition so
        # the pipeline container's own healthcheck invocation only runs
        # after a clean migration.
        dep = self.services["pipeline"]["depends_on"].get("migrate")
        self.assertIsNotNone(dep, "pipeline should depend on the migrate service")
        self.assertEqual(dep["condition"], "service_completed_successfully")

    def test_migrations_use_application_runner_not_copied_init_sql(self):
        migrate = self.services["migrate"]
        # The real CLI subcommand (tuva-postgres migrate), not a copied/
        # forked implementation.
        self.assertIn("migrate", migrate["command"])
        # Never smuggle application-managed schema in through Postgres's
        # own auto-run-on-first-boot init mechanism.
        self.assertNotIn("docker-entrypoint-initdb.d", self.raw_text)

    def test_container_dsn_uses_service_hostname_not_localhost(self):
        for service_name in ("migrate", "pipeline"):
            dsn = self.services[service_name]["environment"]["PG_DSN"]
            self.assertIn("@postgres:5432/", dsn)
            self.assertNotIn("localhost", dsn)
            self.assertNotIn("127.0.0.1", dsn)

    def test_database_startup_and_migration_do_not_require_api_credentials(self):
        for service_name in ("postgres", "migrate"):
            env = self.services[service_name].get("environment") or {}
            self.assertNotIn("TUVA_API_TOKEN", env)
            self.assertNotIn("TUVA_API_MANIFEST_URL", env)

    def test_bare_compose_up_does_not_default_to_full_api_pipeline_run(self):
        # The pipeline service's default command must never be `run`
        # (the full fetch->migrate->load->test pipeline, which requires
        # API credentials) -- `healthcheck` is a safe, DB-only default.
        self.assertEqual(self.services["pipeline"]["command"], ["healthcheck"])

    # --- safety: no privileged access, no docker socket, no secrets --------
    def test_no_privileged_mode_or_docker_socket_mount(self):
        self.assertNotIn("privileged", self.raw_text)
        self.assertNotIn("/var/run/docker.sock", self.raw_text)

    def test_no_obsolete_top_level_version_key(self):
        self.assertNotIn("version", self.doc)

    def test_credentials_clearly_labeled_local_only(self):
        self.assertIn("LOCAL-ONLY", self.raw_text.upper())
        self.assertIn("NOT for production use", self.raw_text)
        self.assertIn("no TLS", self.raw_text)


class TestLocalDbMakefileTargets(unittest.TestCase):
    """Lighter Makefile checks scoped to this module's compose-consistency
    angle (target presence, non-destructive routine shutdown, guarded
    reset). See tests/unit/test_makefile_and_ci.py for the fuller set of
    Makefile-recipe assertions (down/reset -v semantics per target,
    shell/status/migrate command bodies, etc.)."""

    def setUp(self):
        self.text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    def test_required_local_db_targets_exist(self):
        for target in (
            "local-db-up", "local-db-migrate", "local-db-ready", "local-db-status",
            "local-db-shell", "local-db-logs", "local-db-down", "local-db-reset",
            "test-compose-integration",
        ):
            self.assertRegex(self.text, rf"(?m)^{re.escape(target)}:")

    def test_normal_down_targets_preserve_volumes(self):
        down_recipe_match = re.search(r"(?m)^local-db-down:.*?\n((?:\t.*\n?)*)", self.text)
        self.assertIsNotNone(down_recipe_match)
        self.assertNotIn("-v", down_recipe_match.group(1))

    def test_reset_target_is_the_only_one_removing_volumes(self):
        reset_recipe_match = re.search(r"(?m)^local-db-reset:.*?\n((?:\t.*\n?)*)", self.text)
        self.assertIsNotNone(reset_recipe_match)
        self.assertIn("down -v", reset_recipe_match.group(1))

    def test_migrate_status_shell_use_compose_consistently(self):
        for target in ("local-db-migrate", "local-db-status", "local-db-shell", "local-db-up", "local-db-logs"):
            recipe_match = re.search(rf"(?m)^{re.escape(target)}:.*?\n((?:\t.*\n?)*)", self.text)
            self.assertIsNotNone(recipe_match)
            self.assertIn("docker compose", recipe_match.group(1))


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestDocumentationAndEnvExampleConsistency(unittest.TestCase):
    """Cross-checks that README.md and the checked-in environment
    examples actually agree with compose.yaml's real credentials/
    database/schemas -- derived dynamically from the parsed compose
    document rather than hardcoded literals, so this can't silently rot
    if the local defaults ever change together.
    """

    def setUp(self):
        self.compose_doc = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
        self.readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.setup_env_example = (REPO_ROOT / "scripts" / "setup_env.example").read_text(encoding="utf-8")
        self.local_pg_example = (REPO_ROOT / "scripts" / "setup_local_postgres.example").read_text(encoding="utf-8")

    def _postgres_env(self):
        return self.compose_doc["services"]["postgres"]["environment"]

    def _schema_env(self):
        return self.compose_doc["services"]["migrate"]["environment"]

    def test_readme_documents_local_postgres_compose_workflow(self):
        self.assertIn("Local PostgreSQL with Docker Compose", self.readme)
        self.assertIn("make local-db-ready", self.readme)

    def test_env_examples_use_same_user_password_database_as_compose(self):
        env = self._postgres_env()
        for example_name, text in (
            ("scripts/setup_env.example", self.setup_env_example),
            ("scripts/setup_local_postgres.example", self.local_pg_example),
        ):
            self.assertIn(env["POSTGRES_USER"], text, f"{example_name} missing compose's POSTGRES_USER")
            self.assertIn(env["POSTGRES_PASSWORD"], text, f"{example_name} missing compose's POSTGRES_PASSWORD")
            self.assertIn(env["POSTGRES_DB"], text, f"{example_name} missing compose's POSTGRES_DB")

    def test_env_examples_use_host_address_not_service_hostname(self):
        for text in (self.setup_env_example, self.local_pg_example):
            dsn_lines = [line for line in text.splitlines() if line.strip().startswith("export PG_DSN=")]
            self.assertTrue(dsn_lines, "no active (uncommented) PG_DSN export found")
            dsn_line = dsn_lines[0]
            self.assertTrue("127.0.0.1" in dsn_line or "localhost" in dsn_line)
            self.assertNotIn("@postgres:", dsn_line)

    def test_env_examples_schemas_match_compose(self):
        schema_env = self._schema_env()
        for text in (self.setup_env_example, self.local_pg_example):
            for var in ("PG_SCHEMA", "TERMINOLOGY_SCHEMA", "OPS_SCHEMA"):
                expected = schema_env[var]
                self.assertRegex(
                    text,
                    rf'export {var}="{re.escape(expected)}"',
                    f"{var} in example does not match compose.yaml's {expected!r}",
                )

    def test_env_examples_never_contain_a_real_looking_production_secret(self):
        for text in (self.setup_env_example, self.local_pg_example):
            self.assertNotIn("prod-postgres", text)
            self.assertIn("local-only", text.lower())


if __name__ == "__main__":
    unittest.main()
