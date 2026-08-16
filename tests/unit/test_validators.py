"""Standard-library unit tests for tuva_ingest.validators: the structural
record-classification rules that decide whether a record is loaded into
its endpoint's raw table or quarantined. Pure functions, no I/O -- these
tests never touch a database or the filesystem.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

import unittest  # noqa: E402

from tuva_ingest.validators import REASON_CODES, validate_record  # noqa: E402


class TestReasonCodeAllowlist(unittest.TestCase):
    def test_expected_reason_codes_present(self):
        self.assertEqual(
            REASON_CODES,
            frozenset(
                {
                    "record_not_object",
                    "missing_required_field",
                    "invalid_required_type",
                    "invalid_identifier",
                    "invalid_date_format",
                    "schema_validation_failed",
                }
            ),
        )

    def test_every_decision_reason_code_is_in_the_allowlist(self):
        cases = [
            ("eligibility", "not-a-dict"),
            ("eligibility", {}),
            ("eligibility", {"person_id": 123}),
            ("eligibility", {"person_id": "   "}),
            ("medical_claim", {}),
            ("medical_claim", {"claim_id": "c1"}),
            ("medical_claim", {"claim_id": "c1", "claim_line_number": None}),
            ("medical_claim", {"claim_id": "c1", "claim_line_number": []}),
        ]
        for endpoint, record in cases:
            with self.subTest(endpoint=endpoint, record=record):
                decision = validate_record(endpoint, record)
                self.assertIsNotNone(decision)
                self.assertIn(decision.reason_code, REASON_CODES)


class TestRecordNotObject(unittest.TestCase):
    def test_non_dict_record_is_flagged(self):
        for bad in ["a string", 123, None, [1, 2, 3], True]:
            with self.subTest(bad=bad):
                decision = validate_record("eligibility", bad)
                self.assertEqual(decision.reason_code, "record_not_object")


class TestEligibilityRequiredFields(unittest.TestCase):
    def test_valid_record_passes(self):
        self.assertIsNone(validate_record("eligibility", {"person_id": "p-1", "other": "field"}))

    def test_missing_person_id_is_quarantined(self):
        decision = validate_record("eligibility", {"member_id": "m-1"})
        self.assertEqual(decision.reason_code, "missing_required_field")

    def test_null_person_id_is_quarantined(self):
        decision = validate_record("eligibility", {"person_id": None})
        self.assertEqual(decision.reason_code, "missing_required_field")

    def test_non_string_person_id_is_quarantined(self):
        decision = validate_record("eligibility", {"person_id": 12345})
        self.assertEqual(decision.reason_code, "invalid_required_type")

    def test_blank_person_id_is_quarantined(self):
        decision = validate_record("eligibility", {"person_id": "   "})
        self.assertEqual(decision.reason_code, "invalid_identifier")

    def test_optional_fields_being_null_does_not_cause_quarantine(self):
        # member_id/subscriber_id are documented as "expected downstream"
        # but not required at the structural layer -- being null must
        # never by itself cause quarantine (no invented business rule).
        record = {"person_id": "p-1", "member_id": None, "subscriber_id": None}
        self.assertIsNone(validate_record("eligibility", record))


class TestMedicalAndPharmacyClaimRequiredFields(unittest.TestCase):
    def test_valid_medical_claim_record_passes(self):
        record = {"claim_id": "c-1", "claim_line_number": 1}
        self.assertIsNone(validate_record("medical_claim", record))

    def test_valid_pharmacy_claim_record_passes(self):
        record = {"claim_id": "c-1", "claim_line_number": "1"}
        self.assertIsNone(validate_record("pharmacy_claim", record))

    def test_missing_claim_id_is_quarantined(self):
        decision = validate_record("medical_claim", {"claim_line_number": 1})
        self.assertEqual(decision.reason_code, "missing_required_field")

    def test_missing_claim_line_number_is_quarantined(self):
        decision = validate_record("medical_claim", {"claim_id": "c-1"})
        self.assertEqual(decision.reason_code, "missing_required_field")

    def test_non_string_claim_id_is_quarantined(self):
        decision = validate_record("medical_claim", {"claim_id": 999, "claim_line_number": 1})
        self.assertEqual(decision.reason_code, "invalid_required_type")

    def test_object_claim_line_number_is_quarantined(self):
        decision = validate_record("medical_claim", {"claim_id": "c-1", "claim_line_number": {"nested": True}})
        self.assertEqual(decision.reason_code, "invalid_required_type")

    def test_boolean_claim_line_number_is_quarantined(self):
        # bool is technically an int subclass in Python -- explicitly
        # excluded so True/False never pass as a valid claim_line_number.
        decision = validate_record("medical_claim", {"claim_id": "c-1", "claim_line_number": True})
        self.assertEqual(decision.reason_code, "invalid_required_type")

    def test_blank_claim_id_is_quarantined(self):
        decision = validate_record("medical_claim", {"claim_id": "  ", "claim_line_number": 1})
        self.assertEqual(decision.reason_code, "invalid_identifier")


class TestDateShapeValidation(unittest.TestCase):
    def test_present_and_well_shaped_date_passes(self):
        record = {"claim_id": "c-1", "claim_line_number": 1, "claim_start_date": "2025-01-01"}
        self.assertIsNone(validate_record("medical_claim", record))

    def test_present_and_malformed_date_is_quarantined(self):
        record = {"claim_id": "c-1", "claim_line_number": 1, "claim_start_date": "not-a-date"}
        decision = validate_record("medical_claim", record)
        self.assertEqual(decision.reason_code, "invalid_date_format")

    def test_absent_date_field_never_causes_quarantine(self):
        record = {"claim_id": "c-1", "claim_line_number": 1}
        self.assertIsNone(validate_record("medical_claim", record))

    def test_null_date_field_never_causes_quarantine(self):
        record = {"claim_id": "c-1", "claim_line_number": 1, "claim_start_date": None}
        self.assertIsNone(validate_record("medical_claim", record))


class TestUnknownEndpoint(unittest.TestCase):
    def test_endpoint_with_no_registered_rules_only_checks_object_shape(self):
        # validate_record does not itself validate --endpoint against
        # endpoints.SUPPORTED_ENDPOINTS (that happens earlier in the
        # pipeline) -- an unrecognized endpoint simply has no required
        # fields registered, so any JSON object passes.
        self.assertIsNone(validate_record("not-a-real-endpoint", {"anything": "goes"}))


if __name__ == "__main__":
    unittest.main()
