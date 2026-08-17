"""Standard-library unit tests for tuva_ingest.object_storage.keys: exact
object-key construction (vendor, normalized endpoint, UTC load date,
UUID run_id, six-digit page number) and rejection of unsafe/malformed
components. No network, no database, no filesystem beyond what the
stdlib itself needs.
"""
from __future__ import annotations

import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import ObjectKeyError  # noqa: E402
from tuva_ingest.object_storage import keys  # noqa: E402


class TestNormalizeEndpoint(unittest.TestCase):
    def test_hyphenated_cli_form_normalizes_to_snake_case(self):
        self.assertEqual(keys.normalize_endpoint("medical-claims"), "medical_claim")
        self.assertEqual(keys.normalize_endpoint("pharmacy-claims"), "pharmacy_claim")
        self.assertEqual(keys.normalize_endpoint("eligibility"), "eligibility")

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(Exception):
            keys.normalize_endpoint("not-a-real-endpoint")


class TestBuildRunKey(unittest.TestCase):
    def test_exact_key_layout(self):
        run_id = "550e8400-e29b-41d4-a716-446655440000"
        run_key = keys.build_run_key(
            prefix="raw", vendor="acme", endpoint="medical_claim", load_date=date(2026, 8, 14), run_id=run_id,
        )
        self.assertEqual(
            run_key.run_prefix,
            "raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/run_id=550e8400-e29b-41d4-a716-446655440000",
        )
        self.assertEqual(
            run_key.page_key(1),
            "raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/"
            "run_id=550e8400-e29b-41d4-a716-446655440000/page=000001.jsonl.gz",
        )
        self.assertEqual(
            run_key.manifest_key,
            "raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/"
            "run_id=550e8400-e29b-41d4-a716-446655440000/manifest.json",
        )
        self.assertEqual(
            run_key.success_key,
            "raw/vendor=acme/endpoint=medical_claim/load_date=2026-08-14/"
            "run_id=550e8400-e29b-41d4-a716-446655440000/_SUCCESS",
        )

    def test_six_digit_page_numbers(self):
        run_key = keys.build_run_key(
            vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id(),
        )
        self.assertTrue(run_key.page_key(1).endswith("page=000001.jsonl.gz"))
        self.assertTrue(run_key.page_key(42).endswith("page=000042.jsonl.gz"))
        self.assertTrue(run_key.page_key(999999).endswith("page=999999.jsonl.gz"))

    def test_default_prefix_is_raw(self):
        run_key = keys.build_run_key(vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())
        self.assertTrue(run_key.run_prefix.startswith("raw/"))

    def test_prefix_is_configurable(self):
        run_key = keys.build_run_key(
            prefix="landing/zone", vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1),
            run_id=keys.new_run_id(),
        )
        self.assertTrue(run_key.run_prefix.startswith("landing/zone/vendor="))

    def test_page_number_out_of_range_rejected(self):
        run_key = keys.build_run_key(vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())
        with self.assertRaises(ObjectKeyError):
            run_key.page_key(0)
        with self.assertRaises(ObjectKeyError):
            run_key.page_key(1_000_000)
        with self.assertRaises(ObjectKeyError):
            run_key.page_key(-1)


class TestUnsafeComponentRejection(unittest.TestCase):
    def test_rejects_uppercase_vendor(self):
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(vendor="ACME", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())

    def test_rejects_path_traversal_in_vendor(self):
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(vendor="../../etc", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())

    def test_rejects_slash_in_vendor(self):
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(vendor="acme/evil", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())

    def test_rejects_whitespace_in_vendor(self):
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(vendor="acme corp", endpoint="eligibility", load_date=date(2026, 1, 1), run_id=keys.new_run_id())

    def test_rejects_empty_prefix(self):
        with self.assertRaises(ObjectKeyError):
            keys.validate_prefix("")
        with self.assertRaises(ObjectKeyError):
            keys.validate_prefix("///")

    def test_prefix_strips_leading_trailing_slashes(self):
        self.assertEqual(keys.validate_prefix("/raw/"), "raw")

    def test_rejects_invalid_run_id(self):
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1), run_id="not-a-uuid")

    def test_rejects_run_id_with_embedded_endpoint_or_timestamp_shape(self):
        # A run_id must be a true UUID -- something that merely LOOKS
        # like it embeds a timestamp/endpoint is still rejected unless it
        # actually matches the UUID4 shape.
        with self.assertRaises(ObjectKeyError):
            keys.build_run_key(
                vendor="acme", endpoint="eligibility", load_date=date(2026, 1, 1),
                run_id="eligibility-20260101T000000-abc123",
            )


class TestNewRunId(unittest.TestCase):
    def test_new_run_id_is_a_valid_uuid(self):
        run_id = keys.new_run_id()
        self.assertEqual(keys.validate_run_id(run_id), run_id)

    def test_new_run_id_is_unique(self):
        self.assertNotEqual(keys.new_run_id(), keys.new_run_id())

    def test_new_run_id_never_embeds_endpoint_or_timestamp(self):
        run_id = keys.new_run_id()
        self.assertNotIn("eligibility", run_id)
        self.assertNotIn("-2026", run_id)  # not a strict proof, but a smoke check


class TestUtcLoadDate(unittest.TestCase):
    def test_computed_from_aware_utc_datetime(self):
        moment = datetime(2026, 8, 14, 23, 59, 0, tzinfo=timezone.utc)
        self.assertEqual(keys.utc_load_date(moment), date(2026, 8, 14))

    def test_rejects_naive_datetime(self):
        with self.assertRaises(ObjectKeyError):
            keys.utc_load_date(datetime(2026, 8, 14, 23, 59, 0))

    def test_default_uses_now(self):
        result = keys.utc_load_date()
        self.assertEqual(result, datetime.now(timezone.utc).date())


if __name__ == "__main__":
    unittest.main()
