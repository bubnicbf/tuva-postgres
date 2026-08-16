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


if __name__ == "__main__":
    unittest.main()
