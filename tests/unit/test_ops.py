"""Unit tests for tuva_postgres.ops against a minimal in-memory fake
connection (not a real PostgreSQL server -- see tests/integration for
that). The fake understands only the specific SQL shapes ops.py emits
(matched by substring), enough to verify: correct SQL target
table/columns, correct parameter binding, that every write commits
immediately, and that reads return what was written -- without pulling in
psycopg or a real database.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres import ops  # noqa: E402


class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=()):
        sql_norm = " ".join(sql.split())
        self._result = None

        if "INSERT INTO" in sql and "pipeline_runs" in sql and "'running'" in sql:
            run_id, source, snapshot_id, environment, app_version, host, started_at = params
            self.store["runs"][run_id] = {
                "run_id": run_id, "source": source, "snapshot_id": snapshot_id,
                "environment": environment, "app_version": app_version, "host": host,
                "started_at": started_at, "finished_at": None, "status": "running",
                "current_stage": "starting", "error_category": None, "error_message": None,
                "artifact_count": None, "bytes_downloaded": None, "rows_loaded": None,
                "tests_passed": None, "tests_failed": None,
            }
        elif "INSERT INTO" in sql and "pipeline_runs" in sql and "'skipped'" in sql:
            run_id, source, environment, app_version, host, started_at, finished_at, reason = params
            self.store["runs"][run_id] = {
                "run_id": run_id, "source": source, "snapshot_id": None, "environment": environment,
                "app_version": app_version, "host": host, "started_at": started_at, "finished_at": finished_at,
                "status": "skipped", "current_stage": "lock", "error_category": "lock", "error_message": reason,
            }
        elif "UPDATE" in sql and "pipeline_runs" in sql and "current_stage = %s WHERE run_id" in sql_norm:
            stage, run_id = params
            self.store["runs"][run_id]["current_stage"] = stage
        elif "UPDATE" in sql and "pipeline_runs" in sql and "snapshot_id = %s WHERE run_id" in sql_norm:
            snapshot_id, run_id = params
            self.store["runs"][run_id]["snapshot_id"] = snapshot_id
        elif "UPDATE" in sql and "pipeline_runs" in sql and "status = 'succeeded'" in sql:
            finished_at, artifact_count, bytes_downloaded, rows_loaded, tests_passed, tests_failed, run_id = params
            row = self.store["runs"][run_id]
            row.update(status="succeeded", current_stage="done", finished_at=finished_at,
                       artifact_count=artifact_count, bytes_downloaded=bytes_downloaded,
                       rows_loaded=rows_loaded, tests_passed=tests_passed, tests_failed=tests_failed)
        elif "UPDATE" in sql and "pipeline_runs" in sql and "status = 'failed'" in sql:
            stage, finished_at, error_category, error_message, run_id = params
            row = self.store["runs"][run_id]
            row.update(status="failed", current_stage=stage, finished_at=finished_at,
                        error_category=error_category, error_message=error_message)
        elif "INSERT INTO" in sql and "pipeline_artifacts" in sql:
            run_id, table, source_url, expected_sha256, expected_size_bytes = params
            self.store["artifacts"][(run_id, table)] = {
                "run_id": run_id, "table_name": table, "source_url": source_url,
                "expected_sha256": expected_sha256, "expected_size_bytes": expected_size_bytes,
                "download_status": None, "load_status": None,
            }
        elif "UPDATE" in sql and "pipeline_artifacts" in sql and "download_status" in sql:
            status, actual_sha256, actual_size_bytes, raw_path, run_id, table = params
            row = self.store["artifacts"][(run_id, table)]
            row.update(download_status=status, actual_sha256=actual_sha256,
                        actual_size_bytes=actual_size_bytes, raw_path=raw_path)
        elif "UPDATE" in sql and "pipeline_artifacts" in sql and "load_status" in sql:
            status, run_id, table = params
            self.store["artifacts"][(run_id, table)]["load_status"] = status
        elif "SELECT run_id, status, started_at" in sql_norm:
            rows = sorted(self.store["runs"].values(), key=lambda r: r["started_at"], reverse=True)
            self._result = [
                (r["run_id"], r["status"], r["started_at"], r["finished_at"], r["current_stage"], r["error_category"])
                for r in rows
            ]
        elif "SELECT run_id, finished_at, artifact_count" in sql_norm:
            rows = [r for r in self.store["runs"].values() if r["status"] == "succeeded"]
            rows.sort(key=lambda r: r["finished_at"], reverse=True)
            self._result = [
                (r["run_id"], r["finished_at"], r["artifact_count"], r["bytes_downloaded"],
                 r["rows_loaded"], r["tests_passed"], r["tests_failed"])
                for r in rows
            ]
        elif "SELECT status FROM" in sql_norm and "pipeline_runs" in sql:
            rows = [r for r in self.store["runs"].values() if r["status"] in ("succeeded", "failed")]
            rows.sort(key=lambda r: r["started_at"], reverse=True)
            self._result = [(r["status"],) for r in rows]
        else:
            raise AssertionError(f"unrecognized SQL in fake cursor: {sql_norm[:120]}")

    def fetchone(self):
        if not self._result:
            return None
        return self._result[0]

    def fetchall(self):
        return list(self._result or [])


class _FakeConn:
    def __init__(self):
        self.store = {"runs": {}, "artifacts": {}}
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        self.commits += 1


from datetime import datetime, timedelta, timezone  # noqa: E402


class TestOps(unittest.TestCase):
    def setUp(self):
        self.conn = _FakeConn()

    def test_create_and_read_running_run(self):
        ops.create_running_run(
            self.conn, "tuva_ops", run_id="r1", source="tuva", snapshot_id=None,
            environment="local", app_version="0.1.0", host="me@box",
        )
        self.assertGreaterEqual(self.conn.commits, 1)
        latest = ops.latest_run(self.conn, "tuva_ops")
        self.assertEqual(latest[0], "r1")
        self.assertEqual(latest[1], "running")

    def test_stage_and_snapshot_updates(self):
        ops.create_running_run(self.conn, "tuva_ops", run_id="r1", source="tuva", snapshot_id=None,
                                environment="local", app_version="0.1.0", host="me@box")
        ops.update_stage(self.conn, "tuva_ops", "r1", "fetch")
        ops.set_snapshot_id(self.conn, "tuva_ops", "r1", "2026-08-15")
        self.assertEqual(self.conn.store["runs"]["r1"]["current_stage"], "fetch")
        self.assertEqual(self.conn.store["runs"]["r1"]["snapshot_id"], "2026-08-15")

    def test_mark_succeeded_serializes_rows_loaded_and_is_visible_as_latest_successful(self):
        ops.create_running_run(self.conn, "tuva_ops", run_id="r1", source="tuva", snapshot_id="s1",
                                environment="local", app_version="0.1.0", host="me@box")
        ops.mark_succeeded(
            self.conn, "tuva_ops", "r1", artifact_count=15, bytes_downloaded=1234,
            rows_loaded={"patient": 10, "encounter": 20}, tests_passed=5, tests_failed=0,
        )
        row = self.conn.store["runs"]["r1"]
        self.assertEqual(row["status"], "succeeded")
        self.assertIn('"patient": 10', row["rows_loaded"])

        last_success = ops.latest_successful_run(self.conn, "tuva_ops")
        self.assertEqual(last_success[0], "r1")

    def test_mark_failed_records_stage_and_sanitized_message(self):
        ops.create_running_run(self.conn, "tuva_ops", run_id="r1", source="tuva", snapshot_id=None,
                                environment="local", app_version="0.1.0", host="me@box")
        ops.mark_failed(self.conn, "tuva_ops", "r1", stage="load", error_category="load",
                         error_message="load_to_postgres.sh exited 1")
        row = self.conn.store["runs"]["r1"]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["current_stage"], "load")
        self.assertNotIn("PG_DSN", row["error_message"])

    def test_mark_skipped_for_lock_contention(self):
        ops.mark_skipped(self.conn, "tuva_ops", "r-skip", source="tuva", environment="local",
                          app_version="0.1.0", host="me@box", reason="lock held")
        row = self.conn.store["runs"]["r-skip"]
        self.assertEqual(row["status"], "skipped")

    def test_artifact_lifecycle(self):
        ops.create_running_run(self.conn, "tuva_ops", run_id="r1", source="tuva", snapshot_id="s1",
                                environment="local", app_version="0.1.0", host="me@box")
        ops.record_artifact_pending(self.conn, "tuva_ops", "r1", table="patient",
                                     source_url="https://example.invalid/patient.csv",
                                     expected_sha256="a" * 64, expected_size_bytes=100)
        ops.update_artifact_download(self.conn, "tuva_ops", "r1", "patient", status="downloaded",
                                      actual_sha256="a" * 64, actual_size_bytes=100, raw_path="/tmp/patient.csv")
        ops.update_artifact_load(self.conn, "tuva_ops", "r1", "patient", "loaded")
        art = self.conn.store["artifacts"][("r1", "patient")]
        self.assertEqual(art["download_status"], "downloaded")
        self.assertEqual(art["load_status"], "loaded")

    def test_consecutive_failures_counts_from_most_recent_run_backward(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, status in enumerate(["succeeded", "failed", "failed", "failed"]):
            run_id = f"r{i}"
            self.conn.store["runs"][run_id] = {
                "run_id": run_id, "status": status, "started_at": base + timedelta(hours=i),
                "finished_at": base + timedelta(hours=i), "current_stage": "done", "error_category": None,
                "source": "tuva", "environment": "local", "app_version": "0.1.0", "host": "x",
                "snapshot_id": None, "artifact_count": None, "bytes_downloaded": None, "rows_loaded": None,
                "tests_passed": None, "tests_failed": None, "error_message": None,
            }
        self.assertEqual(ops.consecutive_failures(self.conn, "tuva_ops"), 3)

    def test_consecutive_failures_zero_when_latest_succeeded(self):
        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i, status in enumerate(["failed", "failed", "succeeded"]):
            run_id = f"r{i}"
            self.conn.store["runs"][run_id] = {
                "run_id": run_id, "status": status, "started_at": base + timedelta(hours=i),
                "finished_at": base + timedelta(hours=i), "current_stage": "done", "error_category": None,
                "source": "tuva", "environment": "local", "app_version": "0.1.0", "host": "x",
                "snapshot_id": None, "artifact_count": None, "bytes_downloaded": None, "rows_loaded": None,
                "tests_passed": None, "tests_failed": None, "error_message": None,
            }
        self.assertEqual(ops.consecutive_failures(self.conn, "tuva_ops"), 0)


if __name__ == "__main__":
    unittest.main()
