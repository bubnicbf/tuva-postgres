"""Standard-library unit tests for tuva_ingest.object_raw_loader's
database-free logic: per-record classification (accept vs. reject, and
why) and the source/accepted/inserted/duplicate reconciliation checks.
The COPY-to-temp-table-then-merge SQL itself requires a real psycopg
connection and is covered by tests/integration/test_pipeline_integration.py.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.endpoint_contract import RejectReason  # noqa: E402
from tuva_ingest.errors import ReconciliationError  # noqa: E402
from tuva_ingest.object_raw_loader import PageLoadCounts, _classify_record, _reconcile  # noqa: E402


class TestClassifyRecord(unittest.TestCase):
    def test_accepts_well_formed_eligibility_record(self):
        classified = _classify_record("eligibility", {"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z"})
        self.assertIsNone(classified.rejected)
        self.assertEqual(classified.accepted["_source_record_id"], "p1")
        self.assertEqual(classified.source_record_id, "p1")

    def test_rejects_non_object_record(self):
        classified = _classify_record("eligibility", ["not", "an", "object"])
        self.assertIsNotNone(classified.rejected)
        self.assertEqual(classified.rejected.reason, RejectReason.NOT_AN_OBJECT)
        self.assertIsNone(classified.source_record_id)

    def test_rejects_missing_source_id(self):
        classified = _classify_record("eligibility", {"updated_at": "2026-08-14T00:00:00Z"})
        self.assertEqual(classified.rejected.reason, RejectReason.MISSING_SOURCE_ID)
        self.assertIsNone(classified.source_record_id)

    def test_rejects_missing_timestamp_but_retains_derived_source_id(self):
        # The id was safely derivable even though the record is ultimately
        # rejected for a different reason -- must be captured "when safely
        # available" (docs/SOURCE_CONTRACT.md "Rejected records").
        classified = _classify_record("eligibility", {"person_id": "p1"})
        self.assertEqual(classified.rejected.reason, RejectReason.MISSING_SOURCE_TIMESTAMP)
        self.assertEqual(classified.source_record_id, "p1")

    def test_accepted_row_carries_payload_hash_and_raw_payload(self):
        record = {"person_id": "p1", "updated_at": "2026-08-14T00:00:00Z", "member_id": "m1"}
        classified = _classify_record("eligibility", record)
        self.assertIn("_payload_hash", classified.accepted)
        self.assertEqual(classified.accepted["_raw_payload"], record)
        self.assertEqual(classified.accepted["_source_endpoint"], "eligibility")


class TestReconcile(unittest.TestCase):
    def test_passes_when_counts_balance(self):
        counts = PageLoadCounts(
            page_number=1, source_record_count=10, accepted_count=8, rejected_count=2, inserted_count=6,
            duplicate_count=2,
        )
        _reconcile(counts)  # must not raise

    def test_raises_when_accepted_plus_rejected_mismatch_source_count(self):
        counts = PageLoadCounts(page_number=1, source_record_count=10, accepted_count=8, rejected_count=1)
        with self.assertRaises(ReconciliationError):
            _reconcile(counts)

    def test_raises_when_inserted_plus_duplicate_mismatch_accepted_count(self):
        counts = PageLoadCounts(
            page_number=1, source_record_count=10, accepted_count=10, rejected_count=0, inserted_count=5,
            duplicate_count=4,
        )
        with self.assertRaises(ReconciliationError):
            _reconcile(counts)


if __name__ == "__main__":
    unittest.main()
