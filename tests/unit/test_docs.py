"""Structural checks on docs/RUNBOOK.md: every topic the runbook is
required to cover is actually present, and it doesn't overclaim an
external alerting system that doesn't exist."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))


class TestRunbook(unittest.TestCase):
    def setUp(self):
        self.text = (REPO_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")

    def test_covers_every_required_topic(self):
        for phrase in (
            "Required secrets and configuration",
            "Initial migration",
            "Manual run",
            "Scheduled run",
            "Healthcheck",
            "Viewing structured logs",
            "Querying latest runs",
            "Finding failed artifacts",
            "Retrying a failed snapshot",
            "Checksum mismatch handling",
            "Migration failure handling",
            "Image rollback",
            "Raw snapshot retention",
            "Database backup expectations",
            "Recommended alerts",
        ):
            self.assertIn(phrase, self.text, f"RUNBOOK.md is missing the {phrase!r} topic")

    def test_does_not_claim_an_alerting_system_is_deployed(self):
        self.assertIn(
            "No external alerting system has been deployed or configured",
            self.text,
        )

    def test_lists_every_required_alert_condition(self):
        for phrase in (
            "No successful run within",
            "Consecutive failures",
            "Migration failure",
            "Checksum mismatch",
            "Partial or rejected manifest",
            "Data-quality test failures",
            "Raw storage nearing capacity",
            "Pipeline duration exceeding",
        ):
            self.assertIn(phrase, self.text, f"RUNBOOK.md is missing the {phrase!r} alert condition")

    def test_never_includes_a_real_looking_secret(self):
        self.assertNotIn("postgresql://postgres:postgres@", self.text)


class TestReadmeReferencesRunbook(unittest.TestCase):
    def test_readme_points_to_runbook_and_deploy_docs(self):
        text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/RUNBOOK.md", text)
        self.assertIn("docs/API_MANIFEST.md", text)
        self.assertIn("deploy/kubernetes/README.md", text)


if __name__ == "__main__":
    unittest.main()
