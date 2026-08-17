"""Standard-library unit tests for tuva_ingest.state's canonical
object-storage-backed operational model (ingestion_run/page/cursor/
rejected_record/schema_observation). Same fake-connection pattern as
test_state.py: no real PostgreSQL connection, proving (a) generated SQL
shape/ON CONFLICT targets, (b) which functions commit vs. never commit,
and (c) CursorError is raised when the optimistic-concurrency UPDATE
matches zero rows. Full round-trip behavior against a real database is
covered by tests/integration/test_pipeline_integration.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import state  # noqa: E402
from tuva_ingest.errors import CursorError  # noqa: E402


class _FakeCursor:
    def __init__(self, log, *, fetchone_result=None, rowcount=1):
        self._log = log
        self._fetchone_result = fetchone_result
        self.rowcount = rowcount
        self.executemany_calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        self._log.append((sql, params))

    def executemany(self, sql, rows):
        self.executemany_calls.append((sql, rows))
        self._log.append((sql, rows))

    def fetchone(self):
        return self._fetchone_result

    def fetchall(self):
        return []


class _FakeConnection:
    def __init__(self, *, fetchone_result=None, rowcount=1):
        self.log: list[tuple[str, tuple]] = []
        self.commits = 0
        self._fetchone_result = fetchone_result
        self._rowcount = rowcount
        self.last_cursor: _FakeCursor | None = None

    def cursor(self):
        self.last_cursor = _FakeCursor(self.log, fetchone_result=self._fetchone_result, rowcount=self._rowcount)
        return self.last_cursor

    def commit(self):
        self.commits += 1


class TestAutoCommittingWrites(unittest.TestCase):
    """create_ingestion_run / mark_run_published / mark_run_load_started /
    mark_run_failed each commit their own write immediately."""

    def test_create_ingestion_run_commits(self):
        conn = _FakeConnection()
        state.create_ingestion_run(
            conn, "ops", run_id="r1", vendor="acme", endpoint="eligibility", load_date="2026-08-14",
            storage_bucket=None, storage_run_prefix="raw/vendor=acme/...", requested_cursor=None,
            app_version="0.1.0", environment="local",
        )
        self.assertEqual(conn.commits, 1)

    def test_mark_run_published_commits(self):
        conn = _FakeConnection()
        state.mark_run_published(conn, "ops", "r1", candidate_cursor="2026-08-14", page_count=1, extracted_count=10)
        self.assertEqual(conn.commits, 1)

    def test_mark_run_load_started_commits(self):
        conn = _FakeConnection()
        state.mark_run_load_started(conn, "ops", "r1")
        self.assertEqual(conn.commits, 1)

    def test_mark_run_failed_commits(self):
        conn = _FakeConnection()
        state.mark_run_failed(conn, "ops", "r1", failure_category="raw_load", failure_message="boom")
        self.assertEqual(conn.commits, 1)


class TestNonCommittingWrites(unittest.TestCase):
    """mark_run_committed / insert_ingestion_page / insert_rejected_records
    / upsert_schema_observations / lock_cursor_for_update / commit_cursor
    NEVER commit -- they must participate in the caller's own
    transaction (see object_raw_loader.load_verified_run)."""

    def test_mark_run_committed_never_commits(self):
        conn = _FakeConnection()
        state.mark_run_committed(conn, "ops", "r1", accepted_count=1, rejected_count=0, inserted_count=1, duplicate_count=0)
        self.assertEqual(conn.commits, 0)

    def test_insert_ingestion_page_never_commits(self):
        conn = _FakeConnection()
        state.insert_ingestion_page(
            conn, "ops", run_id="r1", page_number=1, object_key="raw/.../page=000001.jsonl.gz", checksum="a" * 64,
            compressed_size_bytes=100, source_record_count=1, accepted_count=1, rejected_count=0,
            request_cursor=None, response_cursor=None, next_page_cursor=None, retrieved_at=None, status="loaded",
        )
        self.assertEqual(conn.commits, 0)
        sql, _params = conn.log[0]
        self.assertIn("ON CONFLICT (run_id, page_number) DO UPDATE", sql)

    def test_insert_rejected_records_never_commits_and_no_op_on_empty(self):
        conn = _FakeConnection()
        state.insert_rejected_records(conn, "ops", [])
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.log, [])

        state.insert_rejected_records(
            conn, "ops",
            [{
                "run_id": "r1", "page_number": 1, "record_position": 1, "reason_code": "missing_source_id",
                "detail": "x", "source_record_id": None, "payload_hash": "h", "raw_object_key": "k",
            }],
        )
        self.assertEqual(conn.commits, 0)
        sql, _rows = conn.log[0]
        self.assertIn("ON CONFLICT (run_id, page_number, record_position) DO NOTHING", sql)

    def test_upsert_schema_observations_never_commits_and_no_op_on_empty(self):
        conn = _FakeConnection()
        state.upsert_schema_observations(
            conn, "ops", vendor="acme", endpoint="eligibility", run_id="r1", page_number=1, observations={},
            fingerprint="fp",
        )
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.log, [])

        state.upsert_schema_observations(
            conn, "ops", vendor="acme", endpoint="eligibility", run_id="r1", page_number=1,
            observations={"person_id": {"string"}}, fingerprint="fp",
        )
        self.assertEqual(conn.commits, 0)
        sql, _rows = conn.log[0]
        self.assertIn("occurrence_count = so.occurrence_count + 1", sql)

    def test_lock_cursor_for_update_never_commits(self):
        conn = _FakeConnection(fetchone_result=("2026-08-01", "prior-run", 3))
        result = state.lock_cursor_for_update(conn, "ops", "acme", "eligibility")
        self.assertEqual(conn.commits, 0)
        self.assertEqual(result, ("2026-08-01", "prior-run", 3))
        # FOR UPDATE must be present in the locking SELECT.
        select_sql = [sql for sql, _ in conn.log if "SELECT" in sql][0]
        self.assertIn("FOR UPDATE", select_sql)

    def test_commit_cursor_never_commits_on_success(self):
        conn = _FakeConnection(rowcount=1)
        state.commit_cursor(
            conn, "ops", "acme", "eligibility", committed_cursor="2026-08-14", successful_run_id="r1",
            expected_lock_version=3,
        )
        self.assertEqual(conn.commits, 0)

    def test_commit_cursor_raises_when_lock_version_does_not_match(self):
        conn = _FakeConnection(rowcount=0)
        with self.assertRaises(CursorError):
            state.commit_cursor(
                conn, "ops", "acme", "eligibility", committed_cursor="2026-08-14", successful_run_id="r1",
                expected_lock_version=3,
            )


class TestGetCursor(unittest.TestCase):
    def test_returns_none_when_never_committed(self):
        conn = _FakeConnection(fetchone_result=(None, None, None))
        self.assertIsNone(state.get_cursor(conn, "ops", "acme", "eligibility"))

    def test_returns_none_when_no_row_at_all(self):
        conn = _FakeConnection(fetchone_result=None)
        self.assertIsNone(state.get_cursor(conn, "ops", "acme", "eligibility"))

    def test_returns_committed_cursor(self):
        conn = _FakeConnection(fetchone_result=("2026-08-14", "r1", "2026-08-14T00:00:00Z"))
        result = state.get_cursor(conn, "ops", "acme", "eligibility")
        self.assertEqual(result["committed_cursor"], "2026-08-14")
        self.assertEqual(result["successful_run_id"], "r1")


if __name__ == "__main__":
    unittest.main()
