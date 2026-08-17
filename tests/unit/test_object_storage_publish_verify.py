"""Standard-library unit tests for object_storage.publish/verify:
immutable publication ordering (pages -> manifest -> success marker),
refusal to load a run missing/incomplete success marker, refusal to
overwrite immutable objects with different content, and independent
checksum/gzip/record-count/manifest-reconciliation verification at load
time. Uses object_storage.memory.InMemoryBackend -- deterministic, no
filesystem or network.
"""
from __future__ import annotations

import gzip
import json
import sys
import unittest
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import ImmutableObjectError, ObjectVerificationError, RunNotPublishedError  # noqa: E402
from tuva_ingest.object_storage.keys import build_run_key, new_run_id  # noqa: E402
from tuva_ingest.object_storage.memory import InMemoryBackend  # noqa: E402
from tuva_ingest.object_storage.publish import RunPublisher  # noqa: E402
from tuva_ingest.object_storage.verify import load_and_verify_manifest  # noqa: E402


def _run_key():
    return build_run_key(vendor="acme", endpoint="medical_claim", load_date=date(2026, 8, 14), run_id=new_run_id())


class TestPublicationOrdering(unittest.TestCase):
    def setUp(self):
        self.backend = InMemoryBackend()
        self.run_key = _run_key()
        self.publisher = RunPublisher(self.backend, self.run_key)

    def test_pages_published_before_manifest_before_success(self):
        page = self.publisher.publish_page(
            1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None,
            next_page_cursor=None,
        )
        self.assertTrue(self.backend.exists(page.object_key))
        self.assertFalse(self.backend.exists(self.run_key.manifest_key))
        self.assertFalse(self.backend.exists(self.run_key.success_key))

        manifest = self.publisher.publish_manifest(
            vendor="acme", endpoint="medical_claim", requested_cursor=None, candidate_cursor="2026-08-14",
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        self.assertTrue(self.backend.exists(self.run_key.manifest_key))
        self.assertFalse(self.backend.exists(self.run_key.success_key))

        self.publisher.publish_success(manifest)
        self.assertTrue(self.backend.exists(self.run_key.success_key))

    def test_full_round_trip_verifies_successfully(self):
        page = self.publisher.publish_page(
            1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None,
            next_page_cursor=None,
        )
        manifest = self.publisher.publish_manifest(
            vendor="acme", endpoint="medical_claim", requested_cursor=None, candidate_cursor="2026-08-14",
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        self.publisher.publish_success(manifest)

        verified = load_and_verify_manifest(self.backend, self.run_key)
        self.assertEqual(verified.manifest["run_id"], self.run_key.run_id)
        self.assertEqual(verified.manifest["total_record_count"], 1)

    def test_republishing_same_page_content_is_a_safe_no_op(self):
        records = [{"claim_id": "1", "claim_line_number": 1}]
        first = self.publisher.publish_page(1, records, request_cursor=None, response_cursor=None, next_page_cursor=None)
        second = self.publisher.publish_page(1, records, request_cursor=None, response_cursor=None, next_page_cursor=None)
        self.assertEqual(first.sha256, second.sha256)

    def test_republishing_page_with_different_content_raises(self):
        self.publisher.publish_page(1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None, next_page_cursor=None)
        with self.assertRaises(ImmutableObjectError):
            self.publisher.publish_page(1, [{"claim_id": "2", "claim_line_number": 1}], request_cursor=None, response_cursor=None, next_page_cursor=None)


class TestVerificationRefusals(unittest.TestCase):
    def setUp(self):
        self.backend = InMemoryBackend()
        self.run_key = _run_key()
        self.publisher = RunPublisher(self.backend, self.run_key)

    def test_missing_success_marker_refused(self):
        page = self.publisher.publish_page(1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None, next_page_cursor=None)
        self.publisher.publish_manifest(
            vendor="acme", endpoint="medical_claim", requested_cursor=None, candidate_cursor="2026-08-14",
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        # No publish_success() call.
        with self.assertRaises(RunNotPublishedError):
            load_and_verify_manifest(self.backend, self.run_key)

    def test_missing_manifest_refused_even_with_pages_present(self):
        self.publisher.publish_page(1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None, next_page_cursor=None)
        with self.assertRaises(RunNotPublishedError):
            load_and_verify_manifest(self.backend, self.run_key)

    def _publish_complete_run(self):
        page = self.publisher.publish_page(1, [{"claim_id": "1", "claim_line_number": 1}], request_cursor=None, response_cursor=None, next_page_cursor=None)
        manifest = self.publisher.publish_manifest(
            vendor="acme", endpoint="medical_claim", requested_cursor=None, candidate_cursor="2026-08-14",
            pages=[page], extraction_started_at="2026-08-14T00:00:00.000000Z",
        )
        self.publisher.publish_success(manifest)
        return page, manifest

    def test_checksum_mismatch_detected(self):
        page, _manifest = self._publish_complete_run()
        # Directly corrupt the stored page bytes via a fresh InMemoryBackend
        # write is impossible (immutable) -- instead simulate corruption by
        # writing a manifest that claims a different sha256 for this page,
        # which is exactly what an independent re-verification must catch.
        # We do this by hand-crafting a second backend with tampered data.
        tampered_backend = InMemoryBackend()
        for key in self.backend.list(""):
            tampered_backend.put(key, self.backend.get(key))
        # Overwrite the page contents at the object-storage layer directly
        # (bypassing the immutable publisher) to simulate tampering/corruption.
        tampered_backend._objects[page.object_key] = gzip.compress(b'{"claim_id": "TAMPERED"}\n')

        with self.assertRaises(ObjectVerificationError):
            load_and_verify_manifest(tampered_backend, self.run_key)

    def test_record_count_mismatch_detected(self):
        page, _manifest = self._publish_complete_run()
        tampered_backend = InMemoryBackend()
        for key in self.backend.list(""):
            tampered_backend.put(key, self.backend.get(key))
        extra_records_body = gzip.compress(
            b'{"claim_id": "1", "claim_line_number": 1}\n{"claim_id": "2", "claim_line_number": 1}\n'
        )
        tampered_backend._objects[page.object_key] = extra_records_body
        with self.assertRaises(ObjectVerificationError):
            load_and_verify_manifest(tampered_backend, self.run_key)

    def test_manifest_page_count_reconciliation(self):
        page, manifest = self._publish_complete_run()
        tampered_manifest = dict(manifest)
        tampered_manifest["page_count"] = 999
        tampered_backend = InMemoryBackend()
        for key in self.backend.list(""):
            if key == self.run_key.manifest_key:
                continue
            tampered_backend.put(key, self.backend.get(key))
        body = (json.dumps(tampered_manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        tampered_backend._objects[self.run_key.manifest_key] = body
        # success marker still references the ORIGINAL manifest sha256,
        # so this must fail even before reaching the page_count check.
        with self.assertRaises(ObjectVerificationError):
            load_and_verify_manifest(tampered_backend, self.run_key)


if __name__ == "__main__":
    unittest.main()
