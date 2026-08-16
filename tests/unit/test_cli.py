"""Standard-library unit tests for tuva_ingest.cli: argument parsing,
subcommand wiring, and error-handling/exit-code behavior in main().

Deliberately does not exercise the real _cmd_* implementations (they
require a live database/API/dbt) -- that's covered by
tests/integration/test_pipeline_integration.py. These tests only prove
the CLI's own contract: which subcommands exist, how they're dispatched,
and that a ConnectorError raised by a subcommand becomes a clean,
sanitized, single-line stderr message and exit code 1.
"""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import cli  # noqa: E402
from tuva_ingest.errors import ConfigError  # noqa: E402


class TestBuildParser(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_all_subcommands_registered(self):
        args = self.parser.parse_args(["extract"])
        self.assertEqual(args.func, cli._cmd_extract)

        args = self.parser.parse_args(["load-raw"])
        self.assertEqual(args.func, cli._cmd_load_raw)
        self.assertIsNone(args.snapshot_id)

        args = self.parser.parse_args(["load-raw", "--snapshot-id", "snap-1"])
        self.assertEqual(args.snapshot_id, "snap-1")

        args = self.parser.parse_args(["migrate"])
        self.assertEqual(args.func, cli._cmd_migrate)
        self.assertFalse(args.status)

        args = self.parser.parse_args(["migrate", "--status"])
        self.assertTrue(args.status)

        args = self.parser.parse_args(["dbt", "--", "build"])
        self.assertEqual(args.func, cli._cmd_dbt)

        args = self.parser.parse_args(["run"])
        self.assertEqual(args.func, cli._cmd_run)

        args = self.parser.parse_args(["healthcheck"])
        self.assertEqual(args.func, cli._cmd_healthcheck)

    def test_no_subcommand_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args([])

    def test_unknown_subcommand_is_an_error(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["not-a-real-command"])

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)


class TestMainErrorHandling(unittest.TestCase):
    def test_connector_error_from_subcommand_is_sanitized_and_exits_1(self):
        with mock.patch.object(cli, "_cmd_healthcheck", side_effect=ConfigError("PG_DSN is required but not set")):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = cli.main(["healthcheck"])
        self.assertEqual(code, 1)
        self.assertIn("PG_DSN", stderr.getvalue())

    def test_unhandled_exception_is_not_swallowed(self):
        # Only ConnectorError subclasses are caught by main() -- a bug
        # elsewhere must still surface as a real traceback/exit, not a
        # quiet exit code 1 that hides the actual problem.
        with mock.patch.object(cli, "_cmd_healthcheck", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(RuntimeError):
                cli.main(["healthcheck"])

    def test_successful_command_returns_its_own_exit_code(self):
        with mock.patch.object(cli, "_cmd_healthcheck", return_value=0):
            self.assertEqual(cli.main(["healthcheck"]), 0)
        with mock.patch.object(cli, "_cmd_healthcheck", return_value=1):
            self.assertEqual(cli.main(["healthcheck"]), 1)


if __name__ == "__main__":
    unittest.main()
