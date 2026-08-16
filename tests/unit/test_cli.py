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

import contextlib
import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import cli  # noqa: E402
from tuva_ingest.errors import CliUsageError, ConfigError, RunNotFoundError  # noqa: E402


class TestBuildParser(unittest.TestCase):
    def setUp(self):
        self.parser = cli.build_parser()

    def test_all_subcommands_registered(self):
        args = self.parser.parse_args(["extract", "--endpoint", "eligibility"])
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




class TestExtractLoadSyncParsing(unittest.TestCase):
    """CLI parsing for the three new subcommand forms called out in the
    project's contract:
        tuva-ingest extract --endpoint medical-claims --since 2025-01-01
        tuva-ingest load --run-id 019...
        tuva-ingest sync --endpoint medical-claims
    """

    def setUp(self):
        self.parser = cli.build_parser()

    def test_extract_requires_endpoint(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["extract"])

    def test_extract_rejects_unknown_endpoint_choice(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["extract", "--endpoint", "not-a-real-endpoint"])

    def test_extract_with_endpoint_and_since(self):
        args = self.parser.parse_args(["extract", "--endpoint", "medical-claims", "--since", "2025-01-01"])
        self.assertEqual(args.func, cli._cmd_extract)
        self.assertEqual(args.endpoint, "medical-claims")
        self.assertEqual(args.since, "2025-01-01")

    def test_extract_since_defaults_to_none(self):
        args = self.parser.parse_args(["extract", "--endpoint", "eligibility"])
        self.assertIsNone(args.since)

    def test_load_requires_run_id(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["load"])

    def test_load_with_run_id(self):
        args = self.parser.parse_args(["load", "--run-id", "019abc"])
        self.assertEqual(args.func, cli._cmd_load)
        self.assertEqual(args.run_id, "019abc")

    def test_sync_requires_endpoint(self):
        with self.assertRaises(SystemExit):
            self.parser.parse_args(["sync"])

    def test_sync_with_endpoint_and_since(self):
        args = self.parser.parse_args(["sync", "--endpoint", "pharmacy-claims", "--since", "2025-06-01"])
        self.assertEqual(args.func, cli._cmd_sync)
        self.assertEqual(args.endpoint, "pharmacy-claims")
        self.assertEqual(args.since, "2025-06-01")

    def test_load_raw_still_works_as_backward_compatible_alias(self):
        args = self.parser.parse_args(["load-raw"])
        self.assertEqual(args.func, cli._cmd_load_raw)


class TestSinceAndEndpointValidation(unittest.TestCase):
    """_validate_endpoint/_validate_since reject bad input before any
    HTTP request or SQL statement is issued (see cli.py's docstrings)."""

    def test_valid_endpoint_returned_unchanged(self):
        self.assertEqual(cli._validate_endpoint("eligibility"), "eligibility")

    def test_unknown_endpoint_raises_cli_usage_error(self):
        with self.assertRaises(CliUsageError):
            cli._validate_endpoint("not-a-real-endpoint")

    def test_none_since_returned_unchanged(self):
        self.assertIsNone(cli._validate_since(None))

    def test_valid_iso_date_returned_unchanged(self):
        self.assertEqual(cli._validate_since("2025-01-01"), "2025-01-01")

    def test_malformed_since_raises_cli_usage_error(self):
        with self.assertRaises(CliUsageError) as ctx:
            cli._validate_since("01/01/2025")
        self.assertIn("--since", str(ctx.exception))

    def test_datetime_with_time_component_rejected(self):
        with self.assertRaises(CliUsageError):
            cli._validate_since("2025-01-01T00:00:00Z")

    def test_relative_expression_rejected(self):
        with self.assertRaises(CliUsageError):
            cli._validate_since("yesterday")


class TestPrintJson(unittest.TestCase):
    def test_emits_exactly_one_json_line(self):
        import json

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cli._print_json({"event": "extract", "run_id": "r1", "status": "succeeded"})
        lines = [line for line in buf.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        payload = json.loads(lines[0])
        self.assertEqual(payload["event"], "extract")
        self.assertEqual(payload["run_id"], "r1")


@dataclass
class _FakeLoadConfig:
    raw_data_dir: Path
    source_name: str = "tuva"
    ops_schema: str = "ingest_ops"
    pipeline_environment: str = "test"
    pg_dsn_value: str = "postgresql://user:pass@localhost/db"


class TestRunLoadResolution(unittest.TestCase):
    """_run_load's resolution/validation logic (unresolvable run_id,
    legacy full-manifest snapshot rejection) -- does not require a real
    database since both failure paths raise before `db.connect` is ever
    called."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)
        self.config = _FakeLoadConfig(raw_data_dir=self.raw_data_dir)

        import logging

        self.logger = logging.getLogger("tuva_ingest.tests.test_cli")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def test_unresolvable_run_id_raises_run_not_found(self):
        with self.assertRaises(RunNotFoundError):
            cli._run_load("does-not-exist", config=self.config, logger=self.logger)

    def test_legacy_full_manifest_run_id_is_rejected(self):
        from tuva_ingest.extract import RawSnapshotStore

        store = RawSnapshotStore(self.raw_data_dir, "tuva")
        staging = store.begin_staging("snap-legacy-1")
        for table in ("eligibility", "medical_claim", "pharmacy_claim"):
            (staging / f"{table}.csv").write_text("col\nval\n", encoding="utf-8")
        legacy_manifest = {
            "version": 1, "source": "tuva", "snapshot_id": "snap-legacy-1",
            "created_at": "2026-08-14T06:00:00Z", "artifacts": [],
        }
        checksums = {t: {"sha256": "a" * 64, "size_bytes": 8} for t in ("eligibility", "medical_claim", "pharmacy_claim")}
        store.finalize(staging, "snap-legacy-1", legacy_manifest, checksums)

        with self.assertRaises(RunNotFoundError) as ctx:
            cli._run_load("snap-legacy-1", config=self.config, logger=self.logger)
        self.assertIn("legacy", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
