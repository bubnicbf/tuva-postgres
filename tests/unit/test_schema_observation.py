"""Standard-library unit tests for tuva_ingest.schema_observation:
fingerprint determinism independent of key order, sensitivity to real
shape changes, and array/null/mixed-type/nested-object handling."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.schema_observation import fingerprint, merge_paths, walk_paths  # noqa: E402


class TestFingerprintDeterminism(unittest.TestCase):
    def test_key_order_does_not_affect_fingerprint(self):
        a = {"claim_id": "1", "payer": "acme", "amount": 10}
        b = {"amount": 10, "claim_id": "1", "payer": "acme"}
        self.assertEqual(fingerprint(walk_paths(a)), fingerprint(walk_paths(b)))

    def test_different_shapes_produce_different_fingerprints(self):
        a = {"claim_id": "1"}
        b = {"claim_id": "1", "extra_field": "x"}
        self.assertNotEqual(fingerprint(walk_paths(a)), fingerprint(walk_paths(b)))

    def test_same_shape_different_values_same_fingerprint(self):
        # The fingerprint is over PATHS AND TYPES only, never values.
        a = {"claim_id": "1", "amount": 10}
        b = {"claim_id": "999", "amount": 42}
        self.assertEqual(fingerprint(walk_paths(a)), fingerprint(walk_paths(b)))

    def test_deterministic_across_repeated_calls(self):
        record = {"z": 1, "a": {"y": 2, "b": 3}}
        self.assertEqual(fingerprint(walk_paths(record)), fingerprint(walk_paths(record)))


class TestWalkPaths(unittest.TestCase):
    def test_scalar_types(self):
        observations = walk_paths({"a": "x", "b": 1, "c": 1.5, "d": True, "e": None})
        self.assertEqual(observations["a"], {"string"})
        self.assertEqual(observations["b"], {"number"})
        self.assertEqual(observations["c"], {"number"})
        self.assertEqual(observations["d"], {"boolean"})
        self.assertEqual(observations["e"], {"null"})

    def test_nested_object_dotted_path(self):
        observations = walk_paths({"address": {"city": "Nashville", "zip": "37203"}})
        self.assertIn("address", observations)
        self.assertEqual(observations["address"], {"object"})
        self.assertEqual(observations["address.city"], {"string"})
        self.assertEqual(observations["address.zip"], {"string"})

    def test_array_of_scalars(self):
        observations = walk_paths({"tags": ["a", "b", "c"]})
        self.assertEqual(observations["tags"], {"array"})
        self.assertEqual(observations["tags[]"], {"string"})

    def test_array_of_objects_does_not_duplicate_per_index(self):
        observations = walk_paths({"diagnoses": [{"code": "A1"}, {"code": "B2"}, {"code": "C3"}]})
        self.assertEqual(observations["diagnoses"], {"array"})
        self.assertEqual(observations["diagnoses[]"], {"object"})
        self.assertEqual(observations["diagnoses[].code"], {"string"})
        # Exactly one "diagnoses[].code" path key exists regardless of how
        # many elements are in the array (never one per index).
        self.assertEqual(len([p for p in observations if p.startswith("diagnoses[]")]), 2)

    def test_mixed_type_field_accumulates_both_types(self):
        merged = merge_paths(walk_paths({"value": "text"}), walk_paths({"value": 5}))
        self.assertEqual(merged["value"], {"string", "number"})

    def test_null_recorded_distinctly_not_merged_away(self):
        merged = merge_paths(walk_paths({"value": None}), walk_paths({"value": "text"}))
        self.assertEqual(merged["value"], {"null", "string"})

    def test_root_object_itself_not_recorded_as_a_path(self):
        observations = walk_paths({"a": 1})
        self.assertNotIn("$", observations)


class TestMergePaths(unittest.TestCase):
    def test_union_across_multiple_records(self):
        merged = merge_paths(
            walk_paths({"a": 1}),
            walk_paths({"b": "x"}),
            walk_paths({"a": 2}),
        )
        self.assertEqual(set(merged.keys()), {"a", "b"})
        self.assertEqual(merged["a"], {"number"})


if __name__ == "__main__":
    unittest.main()
