"""Standard-library unit tests for tuva_ingest.endpoint_contract:
canonical JSON hashing (key-order independence), endpoint-specific
source-record-id derivation (including collision-safe composite
encoding), and missing/invalid source-updated-at handling."""
from __future__ import annotations

import sys
import unittest
from datetime import timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.endpoint_contract import (  # noqa: E402
    Rejected,
    RejectReason,
    canonical_json_bytes,
    derive_source_record_id,
    derive_source_updated_at,
    encode_composite_id,
    payload_sha256,
)


class TestCanonicalJsonHashing(unittest.TestCase):
    def test_hash_independent_of_key_order(self):
        a = {"claim_id": "1", "claim_line_number": 1, "payer": "acme"}
        b = {"payer": "acme", "claim_line_number": 1, "claim_id": "1"}
        self.assertEqual(payload_sha256(a), payload_sha256(b))

    def test_hash_sensitive_to_value_change(self):
        a = {"claim_id": "1"}
        b = {"claim_id": "2"}
        self.assertNotEqual(payload_sha256(a), payload_sha256(b))

    def test_hash_sensitive_to_nested_key_order_too(self):
        a = {"outer": {"x": 1, "y": 2}}
        b = {"outer": {"y": 2, "x": 1}}
        self.assertEqual(payload_sha256(a), payload_sha256(b))

    def test_lowercase_hex_output(self):
        digest = payload_sha256({"a": 1})
        self.assertEqual(digest, digest.lower())
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # raises ValueError if not valid hex

    def test_canonical_bytes_are_compact_utf8(self):
        body = canonical_json_bytes({"b": 1, "a": "héllo"})
        self.assertEqual(body, b'{"a":"h\xc3\xa9llo","b":1}')


class TestEncodeCompositeId(unittest.TestCase):
    def test_length_prefixed_encoding_is_unambiguous(self):
        # Naive delimiter concatenation would collide here:
        # "ab"+"-"+"c" == "a"+"-"+"bc" == "ab-c". Length-prefixing must not.
        encoded_1 = encode_composite_id(("ab", "c"))
        encoded_2 = encode_composite_id(("a", "bc"))
        self.assertNotEqual(encoded_1, encoded_2)

    def test_deterministic(self):
        self.assertEqual(encode_composite_id(("x", "y")), encode_composite_id(("x", "y")))

    def test_order_sensitive(self):
        self.assertNotEqual(encode_composite_id(("x", "y")), encode_composite_id(("y", "x")))


class TestDeriveSourceRecordId(unittest.TestCase):
    def test_eligibility_uses_person_id(self):
        result = derive_source_record_id("eligibility", {"person_id": "p1"})
        self.assertEqual(result, "p1")

    def test_eligibility_missing_person_id_rejected(self):
        result = derive_source_record_id("eligibility", {"member_id": "m1"})
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, RejectReason.MISSING_SOURCE_ID)

    def test_medical_claim_uses_composite_id(self):
        result = derive_source_record_id("medical_claim", {"claim_id": "c1", "claim_line_number": 1})
        self.assertEqual(result, encode_composite_id(("c1", "1")))

    def test_medical_claim_missing_claim_line_number_rejected(self):
        result = derive_source_record_id("medical_claim", {"claim_id": "c1"})
        self.assertIsInstance(result, Rejected)

    def test_pharmacy_claim_uses_composite_id(self):
        result = derive_source_record_id("pharmacy_claim", {"claim_id": "c1", "claim_line_number": 2})
        self.assertEqual(result, encode_composite_id(("c1", "2")))

    def test_blank_string_id_rejected(self):
        result = derive_source_record_id("eligibility", {"person_id": "   "})
        self.assertIsInstance(result, Rejected)

    def test_integer_id_coerced_to_string(self):
        result = derive_source_record_id("eligibility", {"person_id": 12345})
        self.assertEqual(result, "12345")

    def test_boolean_id_rejected(self):
        result = derive_source_record_id("eligibility", {"person_id": True})
        self.assertIsInstance(result, Rejected)

    def test_fractional_float_line_number_rejected(self):
        result = derive_source_record_id("medical_claim", {"claim_id": "c1", "claim_line_number": 1.5})
        self.assertIsInstance(result, Rejected)

    def test_integral_float_line_number_accepted(self):
        result = derive_source_record_id("medical_claim", {"claim_id": "c1", "claim_line_number": 1.0})
        self.assertEqual(result, encode_composite_id(("c1", "1")))


class TestDeriveSourceUpdatedAt(unittest.TestCase):
    def test_missing_field_rejected(self):
        result = derive_source_updated_at("eligibility", {})
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, RejectReason.MISSING_SOURCE_TIMESTAMP)

    def test_blank_field_rejected(self):
        result = derive_source_updated_at("eligibility", {"updated_at": "  "})
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, RejectReason.MISSING_SOURCE_TIMESTAMP)

    def test_non_string_field_rejected(self):
        result = derive_source_updated_at("eligibility", {"updated_at": 12345})
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, RejectReason.INVALID_SOURCE_TIMESTAMP)

    def test_malformed_string_rejected(self):
        result = derive_source_updated_at("eligibility", {"updated_at": "not-a-date"})
        self.assertIsInstance(result, Rejected)
        self.assertEqual(result.reason, RejectReason.INVALID_SOURCE_TIMESTAMP)

    def test_never_substitutes_ingestion_time(self):
        # No amount of other fields being present should produce a value
        # when updated_at itself is missing.
        result = derive_source_updated_at("eligibility", {"person_id": "p1", "member_id": "m1"})
        self.assertIsInstance(result, Rejected)

    def test_z_suffix_parsed_as_utc(self):
        result = derive_source_updated_at("eligibility", {"updated_at": "2026-08-14T12:00:00Z"})
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 12)

    def test_naive_timestamp_assumed_utc(self):
        result = derive_source_updated_at("eligibility", {"updated_at": "2026-08-14T12:00:00"})
        self.assertEqual(result.tzinfo, timezone.utc)

    def test_offset_timestamp_converted_to_utc(self):
        result = derive_source_updated_at("eligibility", {"updated_at": "2026-08-14T12:00:00-05:00"})
        self.assertEqual(result.tzinfo, timezone.utc)
        self.assertEqual(result.hour, 17)


if __name__ == "__main__":
    unittest.main()
