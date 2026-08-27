"""Standard-library unit tests for tuva_ingest.state's canonical
object-storage-backed operational model (ingestion_run/page/cursor/
rejected_record/schema_observation). Same fake-connection pattern as
test_state.py: no real PostgreSQL connection, proving (a) generated SQL
shape/ON CONFLICT targets, (b) which functions commit vs. never commit,
(c) CursorError is raised when the optimistic-concurrency UPDATE
matches zero rows, and (d) OperationalStateError is raised whenever a
lifecycle transition or a page upsert affects zero rows / conflicts with
already-recorded immutable data -- a zero-row update must never be
silently treated as success. Full round-trip behavior against a real
database (including that occurrence_count genuinely does not inflate on
a replayed run/page) is covered by
tests/integration/test_object_storage_pipeline_integration.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import state  # noqa: E402
from tuva_ingest.errors import CursorError, OperationalStateError  # noqa: E402


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
        self.rollbacks = 0
        self._fetchone_result = fetchone_result
        self._rowcount = rowcount
        self.last_cursor: _FakeCursor | None = None

    def cursor(self):
        self.last_cursor = _FakeCursor(self.log, fetchone_result=self._fetchone_result, rowcount=self._rowcount)
        return self.last_cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class TestAutoCommittingWrites(unittest.TestCase):
    """create_ingestion_run / mark_run_published / mark_run_load_started /
    mark_run_failed each commit their own write immediately -- and, for
    the three lifecycle transitions that require a specific prior status
    (published/load_started, not create/failed), roll back and raise
    OperationalStateError instead of committing a zero-row update."""

    def test_create_ingestion_run_commits(self):
        conn = _FakeConnection()
        state.create_ingestion_run(
            conn, "ops", run_id="r1", vendor="acme", endpoint="eligibility", load_date="2026-08-14",
            storage_bucket=None, storage_run_prefix="raw/vendor=acme/...", requested_cursor=None,
            app_version="0.1.0", environment="local",
        )
        self.assertEqual(conn.commits, 1)

    def test_mark_run_published_commits_on_success(self):
        conn = _FakeConnection(rowcount=1)
        state.mark_run_published(conn, "ops", "r1", candidate_cursor="2026-08-14", page_count=1, extracted_count=10)
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)

    def test_mark_run_published_raises_and_rolls_back_on_zero_rows(self):
        conn = _FakeConnection(rowcount=0)
        with self.assertRaises(OperationalStateError):
            state.mark_run_published(conn, "ops", "r1", candidate_cursor="2026-08-14", page_count=1, extracted_count=10)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)

    def test_mark_run_load_started_commits_on_success(self):
        conn = _FakeConnection(rowcount=1)
        state.mark_run_load_started(conn, "ops", "r1")
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)

    def test_mark_run_load_started_raises_and_rolls_back_on_zero_rows(self):
        conn = _FakeConnection(rowcount=0)
        with self.assertRaises(OperationalStateError):
            state.mark_run_load_started(conn, "ops", "r1")
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 1)

    def test_mark_run_failed_commits(self):
        conn = _FakeConnection()
        state.mark_run_failed(conn, "ops", "r1", failure_category="raw_load", failure_message="boom")
        self.assertEqual(conn.commits, 1)

    def test_mark_run_failed_zero_rows_is_a_safe_no_op_not_an_error(self):
        # Calling mark_run_failed for an already-terminal run is
        # documented, intentional, idempotent behavior -- unlike the
        # lifecycle-forward transitions above, this must NOT raise.
        conn = _FakeConnection(rowcount=0)
        state.mark_run_failed(conn, "ops", "r1", failure_category="raw_load", failure_message="boom")
        self.assertEqual(conn.commits, 1)
        self.assertEqual(conn.rollbacks, 0)


class TestNonCommittingWrites(unittest.TestCase):
    """mark_run_committed / insert_ingestion_page / insert_rejected_records
    / upsert_schema_observations / lock_cursor_for_update / commit_cursor
    NEVER commit -- they must participate in the caller's own
    transaction (see object_raw_loader.load_verified_run). None of them
    roll back internally either -- transaction-boundary ownership
    belongs entirely to the caller."""

    def test_mark_run_committed_never_commits_on_success(self):
        conn = _FakeConnection(rowcount=1)
        state.mark_run_committed(conn, "ops", "r1", accepted_count=1, rejected_count=0, inserted_count=1, duplicate_count=0)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)

    def test_mark_run_committed_raises_without_committing_or_rolling_back_on_zero_rows(self):
        conn = _FakeConnection(rowcount=0)
        with self.assertRaises(OperationalStateError):
            state.mark_run_committed(conn, "ops", "r1", accepted_count=1, rejected_count=0, inserted_count=1, duplicate_count=0)
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)  # the caller (cli._run_object_load) owns the rollback, not this function

    def test_insert_ingestion_page_never_commits(self):
        conn = _FakeConnection(fetchone_result=(1,))
        state.insert_ingestion_page(
            conn, "ops", run_id="r1", page_number=1, object_key="raw/.../page=000001.jsonl.gz", checksum="a" * 64,
            compressed_size_bytes=100, source_record_count=1, accepted_count=1, rejected_count=0,
            request_cursor=None, response_cursor=None, next_page_cursor=None, retrieved_at=None, status="loaded",
        )
        self.assertEqual(conn.commits, 0)
        sql, _params = conn.log[0]
        self.assertIn("ON CONFLICT (run_id, page_number) DO UPDATE", sql)
        self.assertIn("RETURNING 1", sql)
        # Only mutable verification/load-result columns are ever assigned
        # in the SET list -- object_key/checksum/source_record_count must
        # never appear there (they may only appear in the WHERE guard).
        set_clause = sql.split("DO UPDATE SET", 1)[1].split("WHERE", 1)[0]
        for immutable_column in ("object_key", "checksum", "source_record_count"):
            self.assertNotIn(f"{immutable_column} = EXCLUDED", set_clause)
        self.assertIn("object_key = EXCLUDED.object_key", sql)
        self.assertIn("checksum = EXCLUDED.checksum", sql)
        self.assertIn("source_record_count = EXCLUDED.source_record_count", sql)

    def test_insert_ingestion_page_raises_on_conflicting_immutable_metadata(self):
        # RETURNING produced no row -- the ON CONFLICT ... WHERE guard
        # excluded the update because an existing row's immutable
        # identity disagreed with what this call asserted.
        conn = _FakeConnection(fetchone_result=None)
        with self.assertRaises(OperationalStateError):
            state.insert_ingestion_page(
                conn, "ops", run_id="r1", page_number=1, object_key="raw/.../page=000001.jsonl.gz",
                checksum="b" * 64, compressed_size_bytes=100, source_record_count=1, accepted_count=1,
                rejected_count=0, request_cursor=None, response_cursor=None, next_page_cursor=None,
                retrieved_at=None, status="loaded",
            )
        self.assertEqual(conn.commits, 0)
        self.assertEqual(conn.rollbacks, 0)  # the caller owns rollback, not this function

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
        # The occurrence_count increment must be CONDITIONAL on this
        # call's (run_id, page_number) differing from what is already
        # recorded as the row's last-observed occurrence -- a bare
        # unconditional "+ 1" here would inflate occurrence_count on
        # every replay of the same page (the bug this test guards
        # against; see tests/integration's real-database idempotency
        # proof for the actual replay behavior).
        self.assertIn("occurrence_count = CASE", sql)
        self.assertIn("so.last_observed_run_id IS DISTINCT FROM EXCLUDED.last_observed_run_id", sql)
        self.assertIn("so.last_observed_page_number IS DISTINCT FROM EXCLUDED.last_observed_page_number", sql)
        self.assertIn("THEN so.occurrence_count + 1", sql)
        self.assertIn("ELSE so.occurrence_count", sql)
        self.assertNotIn("occurrence_count = so.occurrence_count + 1", sql)

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
