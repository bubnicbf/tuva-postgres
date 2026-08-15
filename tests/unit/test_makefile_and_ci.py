"""Structural checks on the Makefile and .github/workflows/ci.yml: the
new commit-6 targets exist, CI actually invokes them, and nothing here
ever pushes an image or applies/deploys the Kubernetes manifests."""
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


class TestMakefileTargets(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

    def test_required_targets_present(self):
        for target in (
            "deps", "fetch", "migrate", "migration-status", "pipeline", "health",
            "test-unit", "test-integration", "test-container", "test-deploy",
            "docker-build", "compose-up", "compose-down",
        ):
            self.assertRegex(self.text, rf"(?m)^{re.escape(target)}:", f"missing Makefile target {target!r}")

    def test_preserves_existing_targets(self):
        for target in ("init", "create-db", "load", "test-shell", "test", "check-python-deps", "lint", "fmt"):
            self.assertRegex(self.text, rf"(?m)^{re.escape(target)}:", f"pre-existing target {target!r} was removed")

    def test_pg_requiring_targets_state_disposable_database_requirement(self):
        self.assertIn("test-integration:", self.text)
        self.assertIn("DISPOSABLE PostgreSQL test database", self.text)

    def test_container_and_deploy_targets_never_silently_claim_success(self):
        self.assertIn("SKIPPED: docker is not available", self.text)
        self.assertIn("SKIPPED: kubectl is not available", self.text)

    def test_no_registry_push_or_real_kubectl_apply(self):
        self.assertNotIn("docker push", self.text)
        self.assertNotIn("kubectl apply -f", self.text)  # only --dry-run=client apply is present
        self.assertIn("--dry-run=client", self.text)

    def test_local_postgres_lifecycle_targets_present(self):
        for target in (
            "local-db-up", "local-db-migrate", "local-db-ready", "local-db-status",
            "local-db-shell", "local-db-logs", "local-db-down", "local-db-reset",
            "test-compose-integration",
        ):
            self.assertRegex(self.text, rf"(?m)^{re.escape(target)}:", f"missing Makefile target {target!r}")

    def _recipe(self, target: str) -> str:
        match = re.search(rf"(?m)^{re.escape(target)}:.*?\n((?:\t.*\n?)*)", self.text)
        self.assertIsNotNone(match, f"could not locate recipe body for target {target!r}")
        return match.group(1)

    def test_compose_down_and_local_db_down_never_pass_dash_v(self):
        # Routine shutdown must never delete the Postgres data volume --
        # only the explicit, guarded reset target may use `down -v`.
        for target in ("compose-down", "local-db-down"):
            recipe = self._recipe(target)
            self.assertIn("docker compose down", recipe)
            self.assertNotRegex(recipe, r"down\s+-v\b", f"{target} must not pass -v to 'docker compose down'")

    def test_only_local_db_reset_uses_down_dash_v(self):
        # local-db-reset is the sole ordinary target permitted to delete
        # the local Postgres data volume.
        targets_using_down_v = [
            target
            for target in (
                "compose-up", "compose-down", "local-db-up", "local-db-migrate",
                "local-db-ready", "local-db-status", "local-db-shell", "local-db-logs",
                "local-db-down", "local-db-reset",
            )
            if re.search(r"down\s+-v\b", self._recipe(target))
        ]
        self.assertEqual(targets_using_down_v, ["local-db-reset"])

    def test_local_db_reset_is_visibly_marked_destructive_and_guarded(self):
        # The comment block immediately preceding the target, plus the
        # recipe body itself, must call out that this is destructive.
        target_idx = self.text.index("local-db-reset:")
        preceding_comment_block = self.text[:target_idx].splitlines()[-10:]
        recipe = self._recipe("local-db-reset")
        self.assertTrue(
            any("DESTRUCTIVE" in line for line in preceding_comment_block),
            "local-db-reset's preceding comment block does not say DESTRUCTIVE",
        )
        self.assertIn("PERMANENTLY DELETE", recipe)
        # Guarded: requires either the explicit opt-in var or an
        # interactive confirmation prompt -- never unconditional.
        self.assertIn("CONFIRM_LOCAL_DB_RESET", recipe)
        self.assertIn("read -r -p", recipe)

    def test_local_db_migrate_and_status_use_real_migration_runner_via_compose(self):
        migrate_recipe = self._recipe("local-db-migrate")
        self.assertIn("docker compose run", migrate_recipe)
        self.assertIn("migrate", migrate_recipe)

        status_recipe = self._recipe("local-db-status")
        self.assertIn("docker compose", status_recipe)
        self.assertIn("migrate --status", status_recipe)

    def test_local_db_shell_uses_compose_container_not_host_psql(self):
        recipe = self._recipe("local-db-shell")
        self.assertIn("docker compose exec", recipe)
        self.assertIn("psql", recipe)

    def test_local_db_up_waits_for_health(self):
        recipe = self._recipe("local-db-up")
        self.assertIn("docker compose up", recipe)
        self.assertIn("--wait", recipe)

    def test_local_db_ready_composes_up_then_migrate(self):
        recipe_line = re.search(r"(?m)^local-db-ready:\s*(.*)$", self.text)
        self.assertIsNotNone(recipe_line)
        self.assertIn("local-db-up", recipe_line.group(1))
        self.assertIn("local-db-migrate", recipe_line.group(1))


@unittest.skipUnless(HAVE_YAML, "PyYAML is not installed in this environment")
class TestCiWorkflow(unittest.TestCase):
    def setUp(self):
        self.doc = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
        self.steps = self.doc["jobs"]["sql-tests"]["steps"]
        self.run_commands = " \n ".join(s.get("run", "") for s in self.steps)

    def test_invokes_new_commit6_make_targets(self):
        for target in ("test-unit", "test-integration", "test-container", "test-deploy"):
            self.assertIn(f"make {target}", self.run_commands)

    def test_still_runs_preexisting_steps(self):
        for target in ("check-python-deps", "create-db", "load", "test"):
            self.assertIn(f"make {target}", self.run_commands)

    def test_no_docker_push_or_real_deploy_apply(self):
        self.assertNotIn("docker push", self.run_commands)
        self.assertNotIn("kubectl create", self.run_commands)
        # apply is only ever invoked with --dry-run=client
        for line in self.run_commands.splitlines():
            if "kubectl apply" in line:
                self.assertIn("--dry-run=client", line)

    def test_nightly_schedule_preserved(self):
        # PyYAML's default resolver treats the bare `on:` key as the
        # boolean True (a classic YAML 1.1 gotcha) rather than the string
        # "on" -- look it up accordingly instead of "fixing" the workflow
        # file to quote a key GitHub Actions requires unquoted.
        triggers = self.doc.get("on", self.doc.get(True))
        schedule = triggers["schedule"]
        self.assertEqual(schedule[0]["cron"], "0 6 * * *")


if __name__ == "__main__":
    unittest.main()
