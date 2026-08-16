"""Standard-library unit tests for tuva_ingest.endpoints -- the
--endpoint <-> raw table mapping used by `extract`/`load`/`sync`.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.endpoints import (  # noqa: E402
    ENDPOINT_TABLE_MAP,
    SUPPORTED_ENDPOINTS,
    endpoint_for_table,
    table_for_endpoint,
)
from tuva_ingest.errors import CliUsageError  # noqa: E402
from tuva_ingest.manifest import RAW_TABLES  # noqa: E402


class TestEndpointTableMapping(unittest.TestCase):
    def test_medical_claims_maps_to_medical_claim(self):
        self.assertEqual(table_for_endpoint("medical-claims"), "medical_claim")

    def test_pharmacy_claims_maps_to_pharmacy_claim(self):
        self.assertEqual(table_for_endpoint("pharmacy-claims"), "pharmacy_claim")

    def test_eligibility_maps_to_eligibility(self):
        self.assertEqual(table_for_endpoint("eligibility"), "eligibility")

    def test_unknown_endpoint_rejected(self):
        with self.assertRaises(CliUsageError) as ctx:
            table_for_endpoint("not-a-real-endpoint")
        self.assertIn("not-a-real-endpoint", str(ctx.exception))
        self.assertIn("medical-claims", str(ctx.exception))

    def test_underscored_table_name_is_not_accepted_as_an_endpoint(self):
        # Endpoint names are hyphenated (medical-claims); the underlying
        # table name (medical_claim) is a distinct vocabulary and must
        # not be silently accepted as a --endpoint value.
        with self.assertRaises(CliUsageError):
            table_for_endpoint("medical_claim")

    def test_mapping_is_exactly_bijective_with_raw_tables(self):
        self.assertEqual(sorted(ENDPOINT_TABLE_MAP.values()), sorted(RAW_TABLES))
        self.assertEqual(len(ENDPOINT_TABLE_MAP), len(RAW_TABLES))

    def test_supported_endpoints_sorted_and_complete(self):
        self.assertEqual(SUPPORTED_ENDPOINTS, tuple(sorted(ENDPOINT_TABLE_MAP)))
        self.assertEqual(set(SUPPORTED_ENDPOINTS), {"medical-claims", "pharmacy-claims", "eligibility"})


class TestEndpointForTable(unittest.TestCase):
    def test_round_trips_every_supported_endpoint(self):
        for endpoint in SUPPORTED_ENDPOINTS:
            table = table_for_endpoint(endpoint)
            self.assertEqual(endpoint_for_table(table), endpoint)

    def test_unmanaged_table_rejected(self):
        with self.assertRaises(CliUsageError):
            endpoint_for_table("not_a_managed_table")


if __name__ == "__main__":
    unittest.main()
