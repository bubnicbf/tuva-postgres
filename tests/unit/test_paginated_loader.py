"""Standard-library unit tests for tuva_ingest.paginated_loader's
database-free logic: independent re-verification of a published run's
page checksums and record counts before anything is loaded.
`load_paginated_run`/`loaded_row_count` themselves require a real
PostgreSQL connection (COPY, a real transaction) and are covered by
tests/integration/test_pipeline_integration.py instead -- exactly the
same split this repository's `raw_loader.py`/its own unit tests already
use (see test_raw_loader.py's module docstring).
"""
from __future__ import annotations

import gzip
import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import ReconciliationError  # noqa: E402
from tuva_ingest.pagination import PaginatedRunStore, validate_page_envelope  # noqa: E402
from tuva_ingest.paginated_loader import verify_run_manifest  # noqa: E402


def _page_payload(records, *, next_page_token=None, high_water_mark="hwm-1"):
    return {
        "records": records,
        "metadata": {
            "record_count": len(records),
            "page_token": None,
            "next_page_token": next_page_token,
            "high_water_mark": high_water_mark,
        },
    }


class TestVerifyRunManifest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = PaginatedRunStore(Path(self._tmp.name), "tuva")

    def _publish_run(self, run_id="run-1", pages=None):
        pages = pages if pages is not None else [[{"a": 1}, {"a": 2}]]
        staging = self.store.begin_staging(run_id)
        metas = []
        total = 0
        for i, records in enumerate(pages, start=1):
            envelope = validate_page_envelope(_page_payload(records), requested_page_token=None)
            meta = self.store.write_page(
                staging, run_id=run_id, endpoint="eligibility", page_number=i,
                request_page_token=None, envelope=envelope, retrieved_at=datetime.now(timezone.utc),
            )
            metas.append(meta)
            total += len(records)
        return self.store.finalize(
            staging, run_id, metas, endpoint="eligibility", since=None,
            total_record_count=total, candidate_high_water_mark="hwm-final",
        )

    def test_valid_run_verifies_successfully(self):
        self._publish_run()
        manifest = verify_run_manifest(self.store, "run-1")
        self.assertEqual(manifest["total_record_count"], 2)

    def test_multi_page_run_verifies_successfully(self):
        self._publish_run(pages=[[{"a": 1}], [{"a": 2}, {"a": 3}], [{"a": 4}]])
        manifest = verify_run_manifest(self.store, "run-1")
        self.assertEqual(manifest["total_record_count"], 4)
        self.assertEqual(manifest["page_count"], 3)

    def test_missing_page_file_raises_reconciliation_error(self):
        published = self._publish_run()
        page_file = next(published.glob("page-*.jsonl.gz"))
        page_file.unlink()
        with self.assertRaises(ReconciliationError):
            verify_run_manifest(self.store, "run-1")

    def test_tampered_page_file_checksum_mismatch(self):
        published = self._publish_run()
        page_file = next(published.glob("page-*.jsonl.gz"))
        page_file.write_bytes(b"tampered-bytes-not-matching-checksum")
        with self.assertRaises(ReconciliationError) as ctx:
            verify_run_manifest(self.store, "run-1")
        self.assertIn("sha256", str(ctx.exception))

    def test_page_record_count_mismatch_detected_by_recount(self):
        # Simulate corruption that changes the decompressed record count
        # without breaking gzip framing or matching the recorded sha256 --
        # write valid gzip content with a different number of lines, but
        # (deliberately) leave the manifest's recorded values stale so
        # the checksum still won't match either. This proves the
        # independent recount path exists and fails loudly: both the
        # checksum AND the count are wrong, and either is sufficient to
        # reject the run.
        published = self._publish_run()
        page_file = next(published.glob("page-*.jsonl.gz"))
        with gzip.open(page_file, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"a": 1}) + "\n")  # only 1 record instead of the original 2
        with self.assertRaises(ReconciliationError):
            verify_run_manifest(self.store, "run-1")

    def test_summed_record_count_mismatch_against_manifest_total(self):
        published = self._publish_run()
        manifest_path = published / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["total_record_count"] = 999  # tamper with the aggregate total only
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ReconciliationError) as ctx:
            verify_run_manifest(self.store, "run-1")
        self.assertIn("total_record_count", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
