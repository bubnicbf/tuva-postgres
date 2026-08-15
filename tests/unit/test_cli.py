"""Unit tests for tuva_postgres.cli argument parsing and top-level error
handling. Does not touch a database -- each subcommand's own DB-touching
body is exercised indirectly by test_orchestrator.py/test_healthcheck.py/
test_migrations.py instead."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres import cli  # noqa: E402
from tuva_postgres.errors import ConfigError  # noqa: E402


class TestCliParser(unittest.TestCase):
    def test_all_required_subcommands_are_registered(self):
        parser = cli.build_parser()
        subparsers_action = next(
            a for a in parser._actions if getattr(a, "dest", None) == "command"
        )
        self.assertEqual(
            set(subparsers_action.choices.keys()),
            {"run", "fetch", "migrate", "load", "test", "healthcheck"},
        )

    def test_migrate_accepts_status_and_baseline_flags(self):
        parser = cli.build_parser()
        args = parser.parse_args(["migrate", "--status", "--baseline-existing"])
        self.assertTrue(args.status)
        self.assertTrue(args.baseline_existing)

    def test_load_accepts_optional_snapshot_id(self):
        parser = cli.build_parser()
        args = parser.parse_args(["load", "--snapshot-id", "2026-08-15"])
        self.assertEqual(args.snapshot_id, "2026-08-15")

    def test_no_subcommand_is_an_error(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([])

    def test_version_flag(self):
        parser = cli.build_parser()
        with self.assertRaises(SystemExit) as ctx:
            parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)


class TestCliMainErrorHandling(unittest.TestCase):
    def test_pipeline_error_from_subcommand_is_caught_and_reported(self):
        import contextlib
        import io
        from unittest import mock

        stderr = io.StringIO()
        with mock.patch.object(cli, "_cmd_healthcheck", side_effect=ConfigError("PG_DSN not set")):
            with contextlib.redirect_stderr(stderr):
                result = cli.main(["healthcheck"])

        self.assertEqual(result, 1)
        self.assertIn("config", stderr.getvalue())
        self.assertIn("PG_DSN not set", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
