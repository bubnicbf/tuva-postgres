"""End-to-end unit tests for tuva_postgres.orchestrator.run_pipeline.

The API client and raw landing layer are exercised for real (against an
in-process mock HTTP server -- the same pattern as test_api_client.py --
and a real temp-dir RawLandingLayer), because both are pure-Python/
filesystem components with no PostgreSQL dependency. Everything that
*does* need PostgreSQL (connect, advisory locks, migrations.apply_pending,
the operational bookkeeping in ops.py) is injected via
orchestrator.PipelineDeps as lightweight fakes -- this repository's
sandbox has no psycopg/PostgreSQL available (see tests/integration for
the real-database counterpart, which requires a disposable Postgres).

This still gives genuine coverage of the orchestrator's own logic: stage
sequencing, event emission, the "running" record lifecycle, error
handling/sanitization per stage, advisory-lock skip behavior, SIGTERM
handling, and that secrets are never present in any log line.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import sys
import tempfile
import threading
import unittest
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import requests as _requests  # noqa: F401

    HAVE_REQUESTS = True
except ImportError:
    HAVE_REQUESTS = False

if HAVE_REQUESTS:
    from tuva_postgres import orchestrator
    from tuva_postgres.config import PipelineConfig
    from tuva_postgres.manifest import MANAGED_TABLES

TOKEN = "s3cr3t-orchestrator-test-token"


def _artifact_content(table: str) -> bytes:
    return f"id,val\n1,{table}-a\n2,{table}-b\n".encode("utf-8")


class _ManifestServer:
    """Serves a valid manifest + one small CSV per managed table over
    loopback HTTP -- a stand-in for a real vendor API, per the
    configurable-manifest-contract design (docs/API_MANIFEST.md)."""

    def __init__(self, snapshot_id="2026-08-15", fail_table=None, fail_status=500):
        self.snapshot_id = snapshot_id
        self.fail_table = fail_table
        self.fail_status = fail_status
        self._contents = {t: _artifact_content(t) for t in MANAGED_TABLES}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/manifest.json":
                    body = json.dumps(server._manifest_dict()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                table = self.path.rsplit("/", 1)[-1].removesuffix(".csv")
                if table == server.fail_table:
                    self.send_response(server.fail_status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                content = server._contents[table]
                self.send_response(200)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def _manifest_dict(self) -> dict:
        artifacts = []
        for table, content in self._contents.items():
            artifacts.append({
                "table": table,
                "url": f"{self.base_url}/{table}.csv",
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
            })
        return {
            "version": 1, "source": "tuva", "snapshot_id": self.snapshot_id,
            "created_at": "2026-08-15T00:00:00Z", "artifacts": artifacts,
        }

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}"

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@dataclass
class _RunRecord:
    run_id: str
    status: str = "running"
    stage: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    kwargs: dict | None = None


class _FakeOps:
    """Records every call instead of touching a database."""

    def __init__(self):
        self.runs: dict[str, _RunRecord] = {}
        self.stage_updates: list[tuple[str, str]] = []
        self.artifact_calls: list[tuple] = []
        self.skipped_calls: list[dict] = []

    def create_running_run(self, conn, ops_schema, *, run_id, **kwargs):
        self.runs[run_id] = _RunRecord(run_id=run_id, kwargs=kwargs)

    def update_stage(self, conn, ops_schema, run_id, stage):
        self.stage_updates.append((run_id, stage))
        self.runs[run_id].stage = stage

    def set_snapshot_id(self, conn, ops_schema, run_id, snapshot_id):
        self.runs[run_id].kwargs["snapshot_id"] = snapshot_id

    def mark_succeeded(self, conn, ops_schema, run_id, **kwargs):
        self.runs[run_id].status = "succeeded"
        self.runs[run_id].kwargs.update(kwargs)

    def mark_failed(self, conn, ops_schema, run_id, *, stage, error_category, error_message):
        self.runs[run_id].status = "failed"
        self.runs[run_id].stage = stage
        self.runs[run_id].error_category = error_category
        self.runs[run_id].error_message = error_message

    def mark_skipped(self, conn, ops_schema, run_id, **kwargs):
        self.skipped_calls.append({"run_id": run_id, **kwargs})

    def record_artifact_pending(self, conn, ops_schema, run_id, **kwargs):
        self.artifact_calls.append(("pending", run_id, kwargs))

    def update_artifact_download(self, conn, ops_schema, run_id, table, **kwargs):
        self.artifact_calls.append(("download", run_id, table, kwargs))

    def update_artifact_load(self, conn, ops_schema, run_id, table, status):
        self.artifact_calls.append(("load", run_id, table, status))


class _FakeConn:
    def close(self):
        pass


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_config(tmp_dir: Path, manifest_url: str, source="tuva") -> PipelineConfig:
    env = {
        "TUVA_API_MANIFEST_URL": manifest_url,
        "TUVA_API_TOKEN": TOKEN,
        "TUVA_API_ALLOW_INSECURE_HTTP": "1",
        "RAW_DATA_DIR": str(tmp_dir / "raw"),
        "PG_DSN": "postgresql://user:s3cret-pw@localhost:5432/testdb",
        "PIPELINE_ENVIRONMENT": "test",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        return PipelineConfig.load()
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _run_test_summary_stdout(run_id: str, passed=15, failed=0, total=None) -> str:
    total = total if total is not None else passed + failed
    return f"RUN_ID={run_id}\nsummary|{run_id}|{passed}|{failed}|{total}\n"


@unittest.skipUnless(HAVE_REQUESTS, "requests is not installed in this environment")
class TestOrchestrator(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_dir = Path(self._tmp.name)
        self._servers = []
        self._log_stream = io.StringIO()

    def tearDown(self):
        for s in self._servers:
            s.stop()

    def _server(self, **kwargs) -> _ManifestServer:
        s = _ManifestServer(**kwargs)
        self._servers.append(s)
        return s

    def _capture_logs(self):
        # configure_logging writes JSON lines to stdout by default; patch it
        # to a StringIO we can inspect without depending on process stdout.
        import tuva_postgres.logging_utils as lu

        original = lu.configure_logging

        def patched(level="INFO", stream=None):
            return original(level=level, stream=self._log_stream)

        return mock.patch.object(orchestrator, "configure_logging", patched)

    def _deps(self, *, run_subprocess, apply_pending=None, lock_acquired=True):
        fake_ops = _FakeOps()
        return orchestrator.PipelineDeps(
            connect=lambda dsn: _FakeConn(),
            try_advisory_lock=lambda conn, key: lock_acquired,
            advisory_unlock=lambda conn, key: None,
            apply_pending=apply_pending or (lambda conn, config, **kw: []),
            host_identity=lambda: "tester@sandbox",
            run_subprocess=run_subprocess,
            ops_mod=fake_ops,
            environ={"PATH": os.environ.get("PATH", "")},
        ), fake_ops

    def test_happy_path_marks_run_succeeded_and_emits_required_events(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def run_subprocess(args, **kwargs):
            script = args[-1]
            if script.endswith("load_to_postgres.sh"):
                return _FakeCompletedProcess(returncode=0, stdout="ok", stderr="")
            if script.endswith("run_tests.sh"):
                run_id = kwargs["env"]["RUN_ID"]
                return _FakeCompletedProcess(returncode=0, stdout=_run_test_summary_stdout(run_id), stderr="")
            raise AssertionError(f"unexpected subprocess: {args}")

        deps, fake_ops = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_SUCCESS)
        self.assertEqual(len(fake_ops.runs), 1)
        run = next(iter(fake_ops.runs.values()))
        self.assertEqual(run.status, "succeeded")
        self.assertEqual(run.kwargs["tests_failed"], 0)
        self.assertEqual(run.kwargs["artifact_count"], len(MANAGED_TABLES))
        self.assertEqual(run.kwargs["rows_loaded"]["patient"], 2)

        events = [json.loads(line)["event"] for line in self._log_stream.getvalue().splitlines()]
        for required in (
            "pipeline_started", "pipeline_lock_acquired", "manifest_fetched",
            "artifact_download_started", "artifact_download_completed", "raw_snapshot_published",
            "migration_started", "migration_completed", "load_started", "table_loaded",
            "tests_completed", "pipeline_succeeded",
        ):
            self.assertIn(required, events, f"missing required event {required!r}")

        # snapshot actually landed on disk
        snapshot_dir = config.raw_data_dir / "tuva" / server.snapshot_id
        self.assertTrue((snapshot_dir / "_SUCCESS").is_file())

    def test_no_secrets_in_any_log_line(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def run_subprocess(args, **kwargs):
            script = args[-1]
            if script.endswith("load_to_postgres.sh"):
                return _FakeCompletedProcess(returncode=0)
            run_id = kwargs["env"]["RUN_ID"]
            return _FakeCompletedProcess(returncode=0, stdout=_run_test_summary_stdout(run_id))

        deps, _ = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            orchestrator.run_pipeline(config, deps=deps)

        full_log = self._log_stream.getvalue()
        self.assertNotIn(TOKEN, full_log)
        self.assertNotIn("s3cret-pw", full_log)

    def test_lock_not_acquired_skips_run(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")
        deps, fake_ops = self._deps(run_subprocess=lambda *a, **k: _FakeCompletedProcess(), lock_acquired=False)

        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_SKIPPED)
        self.assertEqual(len(fake_ops.skipped_calls), 1)
        self.assertEqual(len(fake_ops.runs), 0)  # never got far enough to create a running record

    def test_migration_failure_marks_pipeline_failed_with_migrate_stage(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def failing_apply_pending(conn, config, **kw):
            raise RuntimeError("simulated migration failure")

        deps, fake_ops = self._deps(
            run_subprocess=lambda *a, **k: _FakeCompletedProcess(), apply_pending=failing_apply_pending,
        )
        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_FAILURE)
        # the running record is created *after* migrate succeeds, so a migrate
        # failure has no row to mark -- verify via the structured log instead.
        events = [json.loads(line) for line in self._log_stream.getvalue().splitlines()]
        failed_events = [e for e in events if e["event"] == "pipeline_failed"]
        self.assertEqual(len(failed_events), 1)
        self.assertEqual(failed_events[0]["stage"], "migrate")

    def test_download_failure_marks_run_failed_with_fetch_stage(self):
        server = self._server(fail_table="patient", fail_status=500)
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")
        deps, fake_ops = self._deps(run_subprocess=lambda *a, **k: _FakeCompletedProcess())

        # keep retries fast/short for this test
        config.api_max_retries = 1

        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_FAILURE)
        run = next(iter(fake_ops.runs.values()))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "fetch")
        # no partial snapshot should ever be published
        snapshot_dir = config.raw_data_dir / "tuva" / server.snapshot_id
        self.assertFalse((snapshot_dir / "_SUCCESS").exists())

    def test_load_script_nonzero_exit_marks_run_failed_with_load_stage(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def run_subprocess(args, **kwargs):
            script = args[-1]
            if script.endswith("load_to_postgres.sh"):
                return _FakeCompletedProcess(returncode=1, stderr="psql: connection refused")
            return _FakeCompletedProcess(returncode=0)

        deps, fake_ops = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_FAILURE)
        run = next(iter(fake_ops.runs.values()))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "load")

    def test_dq_test_failures_mark_run_failed_with_test_stage(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def run_subprocess(args, **kwargs):
            script = args[-1]
            if script.endswith("load_to_postgres.sh"):
                return _FakeCompletedProcess(returncode=0)
            run_id = kwargs["env"]["RUN_ID"]
            return _FakeCompletedProcess(returncode=0, stdout=_run_test_summary_stdout(run_id, passed=10, failed=2))

        deps, fake_ops = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_FAILURE)
        run = next(iter(fake_ops.runs.values()))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.stage, "test")

    def test_sigterm_during_run_is_recorded_as_interrupted_and_releases_lock(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        unlock_calls = []

        def run_subprocess(args, **kwargs):
            os.kill(os.getpid(), signal.SIGTERM)
            return _FakeCompletedProcess(returncode=0)  # unreachable; signal raises first

        fake_ops = _FakeOps()
        deps = orchestrator.PipelineDeps(
            connect=lambda dsn: _FakeConn(),
            try_advisory_lock=lambda conn, key: True,
            advisory_unlock=lambda conn, key: unlock_calls.append(key),
            apply_pending=lambda conn, config, **kw: [],
            host_identity=lambda: "tester@sandbox",
            run_subprocess=run_subprocess,
            ops_mod=fake_ops,
            environ={"PATH": os.environ.get("PATH", "")},
        )
        with self._capture_logs():
            exit_code = orchestrator.run_pipeline(config, deps=deps)

        self.assertEqual(exit_code, orchestrator.EXIT_FAILURE)
        self.assertEqual(len(unlock_calls), 1)
        run = next(iter(fake_ops.runs.values()))
        self.assertEqual(run.status, "failed")
        self.assertEqual(run.error_category, "interrupted")

    def test_idempotent_refetch_of_identical_snapshot_is_reused_not_redownloaded(self):
        server = self._server()
        config = _make_config(self.tmp_dir, server.base_url + "/manifest.json")

        def run_subprocess(args, **kwargs):
            script = args[-1]
            if script.endswith("load_to_postgres.sh"):
                return _FakeCompletedProcess(returncode=0)
            run_id = kwargs["env"]["RUN_ID"]
            return _FakeCompletedProcess(returncode=0, stdout=_run_test_summary_stdout(run_id))

        deps1, _ = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            first_exit = orchestrator.run_pipeline(config, deps=deps1)
        self.assertEqual(first_exit, orchestrator.EXIT_SUCCESS)

        deps2, fake_ops2 = self._deps(run_subprocess=run_subprocess)
        with self._capture_logs():
            second_exit = orchestrator.run_pipeline(config, deps=deps2)
        self.assertEqual(second_exit, orchestrator.EXIT_SUCCESS)

        reused_statuses = [c for c in fake_ops2.artifact_calls if c[0] == "download" and c[3].get("status") == "reused"]
        self.assertEqual(len(reused_statuses), len(MANAGED_TABLES))


if __name__ == "__main__":
    unittest.main()
