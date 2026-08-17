"""Standard-library unit tests for tuva_ingest.state.

No real PostgreSQL connection required: a minimal fake connection/cursor
records every executed statement and its parameters, so these tests
verify (a) identifiers are validated and safely composed before any SQL
reaches the fake cursor, (b) every write commits, and (c) the
'running'-scoped WHERE clauses that make state transitions safe are
actually present in the generated SQL. Full round-trip behavior against
a real database (state actually persisting, WHERE clauses actually
preventing a double transition) is covered by
tests/integration/test_pipeline_integration.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import state  # noqa: E402
from tuva_ingest.identifiers import InvalidIdentifierError  # noqa: E402


class _FakeCursor:
    def __init__(self, log: list[tuple[str, tuple]]):
        self._log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._log.append((sql, params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self):
        self.log: list[tuple[str, tuple]] = []
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self.log)

    def commit(self):
        self.commits += 1


class TestIdentifierValidationBeforeSql(unittest.TestCase):
    """A hostile ops_schema must never reach the cursor at all."""

    def test_create_running_run_rejects_hostile_schema(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.create_running_run(
                conn,
                "ops; DROP TABLE x",
                run_id="r1",
                source="tuva",
                snapshot_id=None,
                environment="local",
                app_version="0.1.0",
                host="test-host",
            )
        self.assertEqual(conn.log, [])
        self.assertEqual(conn.commits, 0)

    def test_latest_run_rejects_hostile_schema(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.latest_run(conn, "bad schema")
        self.assertEqual(conn.log, [])


class TestWritesCommit(unittest.TestCase):
    def test_create_running_run_commits(self):
        conn = _FakeConnection()
        state.create_running_run(
            conn,
            "ingest_ops",
            run_id="r1",
            source="tuva",
            snapshot_id=None,
            environment="local",
            app_version="0.1.0",
            host="test-host",
        )
        self.assertEqual(conn.commits, 1)
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."ingestion_runs"', sql)
        self.assertIn("'running'", sql)
        self.assertEqual(params[0], "r1")

    def test_mark_succeeded_scoped_to_running_status(self):
        conn = _FakeConnection()
        state.mark_succeeded(conn, "ingest_ops", "r1", rows_loaded={"eligibility": 3}, tables_loaded=["eligibility"])
        sql, params = conn.log[0]
        self.assertIn("status = 'succeeded'", sql)
        self.assertIn("WHERE run_id = %s AND status = 'running'", sql)
        self.assertEqual(params[-1], "r1")
        self.assertEqual(conn.commits, 1)

    def test_mark_failed_scoped_to_running_status(self):
        conn = _FakeConnection()
        state.mark_failed(conn, "ingest_ops", "r1", stage="load_raw", error_category="db", error_message="boom")
        sql, params = conn.log[0]
        self.assertIn("status = 'failed'", sql)
        self.assertIn("WHERE run_id = %s AND status = 'running'", sql)
        self.assertEqual(conn.commits, 1)

    def test_update_stage_scoped_to_running_status(self):
        conn = _FakeConnection()
        state.update_stage(conn, "ingest_ops", "r1", "extracting")
        sql, params = conn.log[0]
        self.assertIn("WHERE run_id = %s AND status = 'running'", sql)
        self.assertEqual(params, ("extracting", "r1"))

    def test_record_table_load_pending_commits(self):
        conn = _FakeConnection()
        state.record_table_load_pending(
            conn, "ingest_ops", "r1", table="eligibility", expected_sha256="a" * 64, expected_size_bytes=10
        )
        self.assertEqual(conn.commits, 1)
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."table_loads"', sql)
        self.assertEqual(params[:2], ("r1", "eligibility"))

    def test_mark_table_load_succeeded_filters_by_run_and_table(self):
        conn = _FakeConnection()
        state.mark_table_load_succeeded(
            conn, "ingest_ops", "r1", "eligibility", row_count=5, actual_sha256="a" * 64, actual_size_bytes=10
        )
        sql, params = conn.log[0]
        self.assertIn("WHERE run_id = %s AND table_name = %s", sql)
        self.assertEqual(params[-2:], ("r1", "eligibility"))

    def test_mark_skipped_writes_terminal_skipped_row(self):
        conn = _FakeConnection()
        state.mark_skipped(
            conn, "ingest_ops", "r1", source="tuva", environment="local", app_version="0.1.0",
            host="h", reason="advisory lock held by another run",
        )
        sql, params = conn.log[0]
        self.assertIn("'skipped'", sql)
        self.assertEqual(conn.commits, 1)


class TestConsecutiveFailures(unittest.TestCase):
    def test_stops_counting_at_first_success(self):
        class _Cursor(_FakeCursor):
            def fetchall(self):
                return [("failed",), ("failed",), ("succeeded",), ("failed",)]

        class _Conn(_FakeConnection):
            def cursor(self):
                return _Cursor(self.log)

        conn = _Conn()
        self.assertEqual(state.consecutive_failures(conn, "ingest_ops"), 2)

    def test_zero_when_most_recent_succeeded(self):
        class _Cursor(_FakeCursor):
            def fetchall(self):
                return [("succeeded",), ("failed",)]

        class _Conn(_FakeConnection):
            def cursor(self):
                return _Cursor(self.log)

        conn = _Conn()
        self.assertEqual(state.consecutive_failures(conn, "ingest_ops"), 0)

    def test_zero_with_no_history(self):
        conn = _FakeConnection()
        self.assertEqual(state.consecutive_failures(conn, "ingest_ops"), 0)


class TestTableLoadRowCounts(unittest.TestCase):
    def test_builds_dict_from_rows(self):
        class _Cursor(_FakeCursor):
            def fetchall(self):
                return [("eligibility", 3), ("medical_claim", 7)]

        class _Conn(_FakeConnection):
            def cursor(self):
                return _Cursor(self.log)

        conn = _Conn()
        result = state.table_load_row_counts(conn, "ingest_ops", "r1")
        self.assertEqual(result, {"eligibility": 3, "medical_claim": 7})




class TestUpsertRunningRun(unittest.TestCase):
    def test_rejects_hostile_schema_before_touching_connection(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.upsert_running_run(
                conn, "ops; DROP TABLE x", run_id="r1", source="tuva", snapshot_id="snap-1",
                endpoint="eligibility", requested_since=None, environment="local",
                app_version="0.1.0", host="test-host",
            )
        self.assertEqual(conn.log, [])
        self.assertEqual(conn.commits, 0)

    def test_commits_and_includes_on_conflict_do_update(self):
        conn = _FakeConnection()
        state.upsert_running_run(
            conn, "ingest_ops", run_id="r1", source="tuva", snapshot_id="snap-1",
            endpoint="eligibility", requested_since="2025-01-01", environment="local",
            app_version="0.1.0", host="test-host",
        )
        self.assertEqual(conn.commits, 1)
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."ingestion_runs"', sql)
        self.assertIn("ON CONFLICT (run_id) DO UPDATE", sql)
        self.assertIn("status = 'running'", sql)
        self.assertEqual(params[0], "r1")
        self.assertIn("eligibility", params)

    def test_resets_finished_at_and_error_fields_on_conflict(self):
        conn = _FakeConnection()
        state.upsert_running_run(
            conn, "ingest_ops", run_id="r1", source="tuva", snapshot_id="snap-1",
            endpoint=None, requested_since=None, environment="local",
            app_version="0.1.0", host="test-host",
        )
        sql, _params = conn.log[0]
        self.assertIn("finished_at = NULL", sql)
        self.assertIn("error_category = NULL", sql)
        self.assertIn("error_message = NULL", sql)


class TestUpsertTableLoadPending(unittest.TestCase):
    def test_rejects_hostile_schema_before_touching_connection(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.upsert_table_load_pending(
                conn, "ops; DROP TABLE x", "r1", table="eligibility",
                expected_sha256="a" * 64, expected_size_bytes=10,
            )
        self.assertEqual(conn.log, [])
        self.assertEqual(conn.commits, 0)

    def test_commits_and_includes_on_conflict_do_update(self):
        conn = _FakeConnection()
        state.upsert_table_load_pending(
            conn, "ingest_ops", "r1", table="eligibility", expected_sha256="a" * 64, expected_size_bytes=10
        )
        self.assertEqual(conn.commits, 1)
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."table_loads"', sql)
        self.assertIn("ON CONFLICT (run_id, table_name) DO UPDATE", sql)
        self.assertEqual(params[:2], ("r1", "eligibility"))

    def test_resets_row_count_and_checksum_fields_on_conflict(self):
        conn = _FakeConnection()
        state.upsert_table_load_pending(
            conn, "ingest_ops", "r1", table="eligibility", expected_sha256="a" * 64, expected_size_bytes=10
        )
        sql, _params = conn.log[0]
        self.assertIn("row_count = NULL", sql)
        self.assertIn("actual_sha256 = NULL", sql)
        self.assertIn("load_status = 'pending'", sql)




class TestGetWatermark(unittest.TestCase):
    def test_rejects_hostile_schema_before_touching_connection(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.get_watermark(conn, "ops; DROP TABLE x", "tuva", "eligibility")
        self.assertEqual(conn.log, [])

    def test_returns_none_when_no_row(self):
        conn = _FakeConnection()
        result = state.get_watermark(conn, "ingest_ops", "tuva", "eligibility")
        self.assertIsNone(result)

    def test_returns_dict_when_row_present(self):
        class _Cursor(_FakeCursor):
            def fetchone(self):
                return ("2025-01-01T00:00:00Z", "run-123", "2025-01-02T00:00:00Z")

        class _Conn(_FakeConnection):
            def cursor(self):
                return _Cursor(self.log)

        conn = _Conn()
        result = state.get_watermark(conn, "ingest_ops", "tuva", "eligibility")
        self.assertEqual(result["high_water_mark"], "2025-01-01T00:00:00Z")
        self.assertEqual(result["successful_run_id"], "run-123")

    def test_queries_scoped_by_source_and_endpoint(self):
        conn = _FakeConnection()
        state.get_watermark(conn, "ingest_ops", "tuva", "eligibility")
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."source_watermarks"', sql)
        self.assertEqual(params, ("tuva", "eligibility"))


class TestCommitWatermark(unittest.TestCase):
    def test_rejects_hostile_schema_before_touching_connection(self):
        conn = _FakeConnection()
        with self.assertRaises(InvalidIdentifierError):
            state.commit_watermark(
                conn, "ops; DROP TABLE x", "tuva", "eligibility",
                high_water_mark="hwm-1", successful_run_id="run-1",
            )
        self.assertEqual(conn.log, [])
        self.assertEqual(conn.commits, 0)

    def test_does_not_commit_itself(self):
        # commit_watermark must be callable inside the caller's existing
        # transaction, sharing its eventual conn.commit() with the data
        # load -- it must never commit on its own.
        conn = _FakeConnection()
        state.commit_watermark(
            conn, "ingest_ops", "tuva", "eligibility",
            high_water_mark="hwm-1", successful_run_id="run-1",
        )
        self.assertEqual(conn.commits, 0)

    def test_upsert_targets_source_and_endpoint_conflict(self):
        conn = _FakeConnection()
        state.commit_watermark(
            conn, "ingest_ops", "tuva", "eligibility",
            high_water_mark="hwm-1", successful_run_id="run-1",
        )
        sql, params = conn.log[0]
        self.assertIn('"ingest_ops"."source_watermarks"', sql)
        self.assertIn("ON CONFLICT (source, endpoint) DO UPDATE", sql)
        self.assertEqual(params[:4], ("tuva", "eligibility", "hwm-1", "run-1"))


if __name__ == "__main__":
    unittest.main()
