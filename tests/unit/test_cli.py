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
from tuva_ingest.errors import CliUsageError, ConfigError, OAuthError, QuarantineError, RunNotFoundError  # noqa: E402


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

    def test_oauth_error_from_subcommand_is_sanitized_and_exits_1(self):
        # OAuthError is a ConnectorError subclass (category "oauth") --
        # main() must handle it the same generic way as any other
        # ConnectorError, with no token/secret leaking into stderr.
        with mock.patch.object(
            cli, "_cmd_extract", side_effect=OAuthError("token endpoint returned an invalid grant")
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = cli.main(["extract", "--endpoint", "eligibility"])
        self.assertEqual(code, 1)
        self.assertIn("[oauth]", stderr.getvalue())
        self.assertIn("invalid grant", stderr.getvalue())

    def test_quarantine_error_from_subcommand_is_sanitized_and_exits_1(self):
        with mock.patch.object(
            cli, "_cmd_load", side_effect=QuarantineError("failed to insert quarantine row, rolling back")
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                code = cli.main(["load", "--run-id", "019abc"])
        self.assertEqual(code, 1)
        self.assertIn("[quarantine]", stderr.getvalue())

    def test_oauth_error_message_never_leaks_a_bearer_token_or_secret(self):
        # Defense-in-depth: even if a bug somewhere put a token-shaped
        # value into an OAuthError's message, main()'s sanitize_error
        # pass must still strip it before it reaches stderr.
        sentinel_secret = "sk-live-sentinel-9f8e7d6c5b4a"
        with mock.patch.object(
            cli, "_cmd_extract", side_effect=OAuthError(f"refresh failed, Authorization: Bearer {sentinel_secret}")
        ):
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                cli.main(["extract", "--endpoint", "eligibility"])
        self.assertNotIn(sentinel_secret, stderr.getvalue())




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


class TestRunPaginatedLoadResolution(unittest.TestCase):
    """_run_paginated_load's pre-database resolution/validation logic
    (unresolvable run_id, corrupted/tampered published run) -- does not
    require a real database since both failure paths raise before
    `db.connect` is ever called (see cli._run_paginated_load: it calls
    `store.is_published`/`verify_run_manifest` before opening a
    connection)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.raw_data_dir = Path(self._tmp.name)
        self.config = _FakeLoadConfig(raw_data_dir=self.raw_data_dir)

        import logging

        self.logger = logging.getLogger("tuva_ingest.tests.test_cli")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def _publish_paginated_run(self, run_id="run-1", endpoint="eligibility", records=None):
        from datetime import datetime, timezone

        from tuva_ingest.pagination import PaginatedRunStore, validate_page_envelope

        records = records if records is not None else [{"a": 1}, {"a": 2}]
        store = PaginatedRunStore(self.raw_data_dir, "tuva")
        staging = store.begin_staging(run_id)
        payload = {
            "records": records,
            "metadata": {"record_count": len(records), "page_token": None, "next_page_token": None, "high_water_mark": "hwm-1"},
        }
        envelope = validate_page_envelope(payload, requested_page_token=None)
        meta = store.write_page(
            staging, run_id=run_id, endpoint=endpoint, page_number=1,
            request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
        )
        return store, store.finalize(
            staging, run_id, [meta], endpoint=endpoint, since=None,
            total_record_count=len(records), candidate_high_water_mark="hwm-1",
        )

    def test_unresolvable_run_id_raises_run_not_found(self):
        with self.assertRaises(RunNotFoundError):
            cli._run_paginated_load("does-not-exist", config=self.config, logger=self.logger)

    def test_tampered_run_is_rejected_before_any_database_connection(self):
        from tuva_ingest.errors import ReconciliationError

        store, published = self._publish_paginated_run()
        page_file = next(published.glob("page-*.jsonl.gz"))
        page_file.write_bytes(b"corrupted-not-matching-checksum")

        with self.assertRaises(ReconciliationError):
            cli._run_paginated_load("run-1", config=self.config, logger=self.logger)


class TestBuildPaginatedApiClientAuthModeSelection(unittest.TestCase):
    """_build_paginated_api_client must select OAuth mode if and only if
    TUVA_OAUTH_TOKEN_URL is configured -- the static-secret path
    (secrets.retrieve_api_credential) is otherwise used unchanged. Both
    branches are exercised with every collaborator mocked so no real
    network/OAuth-server/cloud-secret-manager call is made."""

    def _config(self, *, oauth_token_url=None):
        @dataclass
        class _Config:
            oauth_token_url: str | None = oauth_token_url
            oauth_client_id: str | None = "client-1" if oauth_token_url else None
            oauth_client_secret_value: str | None = "secret-1" if oauth_token_url else None
            oauth_scopes: str | None = None
            oauth_refresh_skew_seconds: float = 60.0
            api_max_retries: int = 3
            api_max_retry_delay_seconds: float = 5.0
            api_max_retry_duration_seconds: float = 120.0

            def httpx_timeout(self):
                return None

        return _Config()

    def test_oauth_mode_selected_when_token_url_is_configured(self):
        import logging

        logger = logging.getLogger("tuva_ingest.tests.test_cli.oauth_mode")
        with (
            mock.patch("tuva_ingest.oauth.OAuthTokenManager") as fake_manager_cls,
            mock.patch("tuva_ingest.api_client.ApiClient") as fake_client_cls,
        ):
            config = self._config(oauth_token_url="https://example.invalid/oauth/token")
            cli._build_paginated_api_client(config, logger)
            fake_manager_cls.assert_called_once()
            fake_client_cls.assert_called_once()
            _, kwargs = fake_client_cls.call_args
            self.assertIn("oauth_manager", kwargs)
            self.assertNotIn("token", kwargs)

    def test_static_secret_mode_selected_when_token_url_is_unset(self):
        import logging

        from pydantic import SecretStr

        from tuva_ingest.secrets import ApiCredential

        logger = logging.getLogger("tuva_ingest.tests.test_cli.static_mode")
        with (
            mock.patch(
                "tuva_ingest.secrets.retrieve_api_credential",
                return_value=ApiCredential(api_token=SecretStr("fake-static-token")),
            ) as fake_retrieve,
            mock.patch("tuva_ingest.oauth.OAuthTokenManager") as fake_manager_cls,
            mock.patch("tuva_ingest.api_client.ApiClient") as fake_client_cls,
        ):
            config = self._config(oauth_token_url=None)
            cli._build_paginated_api_client(config, logger)
            fake_retrieve.assert_called_once()
            fake_manager_cls.assert_not_called()
            fake_client_cls.assert_called_once()
            _, kwargs = fake_client_cls.call_args
            self.assertIn("token", kwargs)
            self.assertNotIn("oauth_manager", kwargs)


class TestSyncStopsBeforeLoadOnExtractionFailure(unittest.TestCase):
    """`sync` must stop immediately (never attempt `load`, never report
    success) if extraction fails -- see cli._cmd_sync's own comment.
    Every collaborator (config loading, the watermark lookup, secret
    retrieval, the API client, extraction itself, and the load step) is
    mocked so this test exercises only `_cmd_sync`'s own control flow,
    never a real database/network/cloud call."""

    def _fake_config(self):
        @dataclass
        class _Config:
            raw_data_dir: Path = Path("/tmp/does-not-matter")
            source_name: str = "tuva"
            ops_schema: str = "ingest_ops"
            pg_dsn_value: str = "postgresql://user:pass@localhost/db"
            api_manifest_url: str = "https://example.invalid/v1/records"
            api_max_pages: int = 100
            api_page_size: int | None = None
            api_max_page_bytes: int = 1024
            api_max_retries: int = 3
            api_max_retry_delay_seconds: float = 5.0
            log_level: str = "INFO"

            def httpx_timeout(self):
                return None

        return _Config()

    def test_extraction_failure_prevents_load_from_ever_being_called(self):
        from pydantic import SecretStr

        from tuva_ingest.errors import PaginationError
        from tuva_ingest.secrets import ApiCredential

        args = cli.build_parser().parse_args(["sync", "--endpoint", "eligibility"])

        with (
            mock.patch.object(cli.IngestConfig, "load", return_value=self._fake_config()),
            mock.patch("tuva_ingest.state.get_watermark", return_value=None),
            mock.patch("tuva_ingest.db.connect", return_value=mock.Mock(close=mock.Mock())),
            mock.patch(
                "tuva_ingest.secrets.retrieve_api_credential",
                return_value=ApiCredential(api_token=SecretStr("fake-token")),
            ),
            mock.patch("tuva_ingest.api_client.ApiClient", return_value=mock.Mock(close=mock.Mock())),
            mock.patch(
                "tuva_ingest.pagination.extract_paginated_run",
                side_effect=PaginationError("simulated extraction failure"),
            ),
            mock.patch.object(cli, "_run_paginated_load") as fake_load,
        ):
            with self.assertRaises(PaginationError):
                cli._cmd_sync(args)

        fake_load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
