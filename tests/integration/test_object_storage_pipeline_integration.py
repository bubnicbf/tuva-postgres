"""End-to-end PostgreSQL integration tests for the object-storage-backed
ingestion workflow (object_extract.py/object_raw_loader.py).

*** Requires a real, DISPOSABLE PostgreSQL database via PG_DSN. ***

Same isolation discipline as test_pipeline_integration.py: every test
creates its own uniquely-suffixed raw/ops schema pair and drops only
those exact schemas on teardown. Object storage itself is always the
deterministic, in-process object_storage.memory.InMemoryBackend here
(never a filesystem or network dependency) -- this suite's job is to
prove the PostgreSQL side of the contract (COPY-to-temp, merge,
reconciliation, cursor safety, rollback, grants) against a REAL
database; object_storage/verify.py's own correctness against the fake
backend is already proven database-free by
tests/unit/test_object_storage_publish_verify.py. See
test_object_storage_minio_integration.py for the opt-in, real-S3-API
counterpart (MinIO via docker compose).

Run:

    PG_DSN=postgresql://user:pass@host:port/db \\
      python3 -m unittest tests.integration.test_object_storage_pipeline_integration -v
"""
from __future__ import annotations

import os
import secrets
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

try:
    import psycopg  # noqa: F401

    HAVE_PSYCOPG = True
except ImportError:
    HAVE_PSYCOPG = False

PG_DSN = os.environ.get("PG_DSN")

_SKIP_REASON = None
if not HAVE_PSYCOPG:
    _SKIP_REASON = "psycopg is not installed in this environment (run `uv sync --locked`)"
elif not PG_DSN:
    _SKIP_REASON = "PG_DSN is not set -- point it at a disposable PostgreSQL database to run this suite"

if _SKIP_REASON:
    print(f"tests.integration.test_object_storage_pipeline_integration: SKIPPED ({_SKIP_REASON})", file=sys.stderr)
else:
    from tuva_ingest import migrations, state  # noqa: E402
    from tuva_ingest.db import connect, qualified_relation  # noqa: E402
    from tuva_ingest.errors import CursorError  # noqa: E402
    from tuva_ingest.object_raw_loader import load_verified_run  # noqa: E402
    from tuva_ingest.object_storage.keys import build_run_key, new_run_id  # noqa: E402
    from tuva_ingest.object_storage.memory import InMemoryBackend  # noqa: E402
    from tuva_ingest.object_storage.publish import RunPublisher  # noqa: E402


def _unique_suffix() -> str:
    return secrets.token_hex(4)


class _TestConfig:
    def __init__(self, *, raw_schema, ops_schema, ingest_role, transform_role, object_storage_prefix="raw"):
        self.raw_schema = raw_schema
        self.ops_schema = ops_schema
        self.ingest_role = ingest_role
        self.transform_role = transform_role
        self.object_storage_prefix = object_storage_prefix


class _ObjectStorageIsolatedTestCase(unittest.TestCase):
    def setUp(self):
        if _SKIP_REASON:
            self.skipTest(_SKIP_REASON)
        suffix = _unique_suffix()
        self.raw_schema = f"raw_test_{suffix}"
        self.ops_schema = f"ops_test_{suffix}"
        self.config = _TestConfig(
            raw_schema=self.raw_schema, ops_schema=self.ops_schema,
            ingest_role=f"ingest_role_{suffix}", transform_role=f"transform_role_{suffix}",
        )
        self.conn = connect(PG_DSN)
        self.addCleanup(self._drop_schemas)
        migrations.apply_pending(self.conn, self.config)
        self.backend = InMemoryBackend()

    def _drop_schemas(self):
        try:
            with self.conn.cursor() as cur:
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.raw_schema}" CASCADE')
                cur.execute(f'DROP SCHEMA IF EXISTS "{self.ops_schema}" CASCADE')
                cur.execute(f'DROP ROLE IF EXISTS "{self.config.ingest_role}"')
                cur.execute(f'DROP ROLE IF EXISTS "{self.config.transform_role}"')
            self.conn.commit()
        finally:
            self.conn.close()

    def _publish_run(self, records, *, endpoint="eligibility", vendor="acme", candidate_cursor="2026-08-14"):
        run_id = new_run_id()
        run_key = build_run_key(
            prefix=self.config.object_storage_prefix, vendor=vendor, endpoint=endpoint,
            load_date=date(2026, 8, 14), run_id=run_id,
        )
        publisher = RunPublisher(self.backend, run_key)
        page = publisher.publish_page(1, records, request_cursor=None, response_cursor=None, next_page_cursor=None)
        manifest = publisher.publish_manifest(
            vendor=vendor, endpoint=endpoint, requested_cursor=None, candidate_cursor=candidate_cursor,
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        publisher.publish_success(manifest)

        state.create_ingestion_run(
            self.conn, self.ops_schema, run_id=run_id, vendor=vendor, endpoint=endpoint, load_date=date(2026, 8, 14),
            storage_bucket=None, storage_run_prefix=run_key.run_prefix, requested_cursor=None,
            app_version="test", environment="test",
        )
        state.mark_run_published(
            self.conn, self.ops_schema, run_id, candidate_cursor=candidate_cursor, page_count=1,
            extracted_count=len(records),
        )
        return run_key

    def _load(self, run_key):
        state.mark_run_load_started(self.conn, self.ops_schema, run_key.run_id)
        result = load_verified_run(self.conn, self.config, self.backend, run_key, logger=None)
        self.conn.commit()
        return result

    def _raw_row_count(self, table="eligibility"):
        relation = qualified_relation(self.raw_schema, table)
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {relation} WHERE _ingestion_run_id IS NOT NULL")
            (count,) = cur.fetchone()
        return count


class TestCopyAndMerge(_ObjectStorageIsolatedTestCase):
    def test_accepted_records_are_inserted(self):
        run_key = self._publish_run([
            {"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"},
            {"person_id": "p2", "updated_at": "2026-08-14T00:00:00Z"},
        ])
        result = self._load(run_key)
        self.assertEqual(result.inserted_count, 2)
        self.assertEqual(result.rejected_count, 0)
        self.assertEqual(self._raw_row_count(), 2)

    def test_rejected_records_do_not_reach_the_raw_table(self):
        run_key = self._publish_run([
            {"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"},
            {"member_id": "no-person-id"},  # missing person_id -> rejected
        ])
        result = self._load(run_key)
        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(self._raw_row_count(), 1)

        relation = qualified_relation(self.ops_schema, "rejected_record")
        with self.conn.cursor() as cur:
            cur.execute(f"SELECT reason_code FROM {relation} WHERE run_id = %s", (run_key.run_id,))
            reasons = [row[0] for row in cur.fetchall()]
        self.assertEqual(reasons, ["missing_source_id"])

    def test_retrying_the_same_run_inserts_no_duplicates(self):
        run_key = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}])
        first = self._load(run_key)
        self.assertEqual(first.inserted_count, 1)

        # Retry: same run_id, same records.
        state.mark_run_load_started(self.conn, self.ops_schema, run_key.run_id)
        second = load_verified_run(self.conn, self.config, self.backend, run_key, logger=None)
        self.conn.commit()
        self.assertEqual(second.inserted_count, 0)
        self.assertEqual(second.duplicate_count, 1)
        self.assertEqual(self._raw_row_count(), 1)

    def test_replaying_same_rows_under_a_different_run_id_inserts_no_duplicates(self):
        records = [{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}]
        run_key_1 = self._publish_run(records)
        self._load(run_key_1)

        run_key_2 = self._publish_run(records)  # fresh run_id, identical logical rows
        result_2 = self._load(run_key_2)
        self.assertEqual(result_2.inserted_count, 0)
        self.assertEqual(result_2.duplicate_count, 1)
        self.assertEqual(self._raw_row_count(), 1)

    def test_changed_payload_same_id_and_timestamp_is_retained_as_new_row(self):
        run_key_1 = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z", "member_id": "m1"}])
        self._load(run_key_1)
        # Same id, same timestamp, DIFFERENT payload (member_id changed) -> different hash -> new row.
        run_key_2 = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z", "member_id": "m2"}])
        result_2 = self._load(run_key_2)
        self.assertEqual(result_2.inserted_count, 1)
        self.assertEqual(self._raw_row_count(), 2)

    def test_changed_source_timestamp_is_retained_as_new_version(self):
        run_key_1 = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}])
        self._load(run_key_1)
        run_key_2 = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-15T00:00:00Z"}], candidate_cursor="2026-08-15")
        result_2 = self._load(run_key_2)
        self.assertEqual(result_2.inserted_count, 1)
        self.assertEqual(self._raw_row_count(), 2)


class TestCursorSafety(_ObjectStorageIsolatedTestCase):
    def test_cursor_and_raw_data_commit_together(self):
        run_key = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}])
        self._load(run_key)
        cursor = state.get_cursor(self.conn, self.ops_schema, "acme", "eligibility")
        self.assertEqual(cursor["committed_cursor"], "2026-08-14")

    def test_backward_cursor_movement_fails(self):
        run_key_1 = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}], candidate_cursor="2026-08-14")
        self._load(run_key_1)

        run_key_2 = self._publish_run([{"person_id": "p2", "updated_at": "2026-08-13T00:00:00Z"}], candidate_cursor="2026-08-13")
        state.mark_run_load_started(self.conn, self.ops_schema, run_key_2.run_id)
        with self.assertRaises(CursorError):
            load_verified_run(self.conn, self.config, self.backend, run_key_2, logger=None)
        self.conn.rollback()

        # The cursor must be unchanged after the rollback.
        cursor = state.get_cursor(self.conn, self.ops_schema, "acme", "eligibility")
        self.assertEqual(cursor["committed_cursor"], "2026-08-14")

    def test_failure_after_merge_before_cursor_update_rolls_back_both(self):
        run_key = self._publish_run([{"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"}], candidate_cursor="2026-08-14")
        self._load(run_key)

        # Simulate a failure between the raw merge and the cursor commit by
        # manually advancing the cursor out from under a second in-flight
        # transaction's expected lock_version, forcing commit_cursor's
        # optimistic-concurrency check to fail (CursorError) -- proving
        # that failure path never leaves a partial raw insert visible.
        run_key_2 = self._publish_run([{"person_id": "p3", "updated_at": "2026-08-16T00:00:00Z"}], candidate_cursor="2026-08-16")
        state.mark_run_load_started(self.conn, self.ops_schema, run_key_2.run_id)

        # Pre-fetch the current lock_version, then bump it behind the
        # scenes (a separate connection committing first) to simulate a
        # concurrent commit racing this transaction.
        other_conn = connect(PG_DSN)
        try:
            state.lock_cursor_for_update(other_conn, self.ops_schema, "acme", "eligibility")
            state.commit_cursor(
                other_conn, self.ops_schema, "acme", "eligibility", committed_cursor="2026-08-15",
                successful_run_id=run_key.run_id, expected_lock_version=0,
            )
            other_conn.commit()
        finally:
            other_conn.close()

        rows_before = self._raw_row_count()
        with self.assertRaises(Exception):
            load_verified_run(self.conn, self.config, self.backend, run_key_2, logger=None)
        self.conn.rollback()
        self.assertEqual(self._raw_row_count(), rows_before)  # the new run's row never became visible


class TestLeastPrivilegeGrants(_ObjectStorageIsolatedTestCase):
    def test_transform_role_can_read_raw_but_not_rejected_record(self):
        run_key = self._publish_run([{"member_id": "no-person-id"}])  # forces a rejected_record row
        self._load(run_key)

        with self.conn.cursor() as cur:
            cur.execute(f'SET ROLE "{self.config.transform_role}"')
            try:
                relation = qualified_relation(self.raw_schema, "eligibility")
                cur.execute(f"SELECT count(*) FROM {relation}")  # must succeed (read-only grant)
                self.assertIsNotNone(cur.fetchone())

                rejected_relation = qualified_relation(self.ops_schema, "rejected_record")
                with self.assertRaises(Exception):
                    cur.execute(f"SELECT count(*) FROM {rejected_relation}")
            finally:
                self.conn.rollback()  # the failed SELECT aborts the transaction
                with self.conn.cursor() as reset_cur:
                    reset_cur.execute("RESET ROLE")
                self.conn.commit()


if __name__ == "__main__":
    unittest.main()
