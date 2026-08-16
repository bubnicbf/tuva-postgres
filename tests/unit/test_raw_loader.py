"""Standard-library unit tests for tuva_ingest.raw_loader's database-free
logic: checksum verification, CSV row iteration, and identifier-safe
relation composition. `load_table`/`load_snapshot` themselves require a
real PostgreSQL connection and are covered by
tests/integration/test_pipeline_integration.py instead.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest.errors import RawLoadError  # noqa: E402
from tuva_ingest.identifiers import InvalidIdentifierError  # noqa: E402
from tuva_ingest.manifest import RAW_TABLES  # noqa: E402
from tuva_ingest.raw_loader import (  # noqa: E402
    _RAW_COLUMNS,
    _iter_csv_rows,
    _relation,
    verify_file_checksum,
)


class TestFixedRawColumns(unittest.TestCase):
    """The raw table column list is fixed and hardcoded -- never derived
    from an untrusted CSV header -- exactly the identifier-injection
    defense raw_loader.py's module docstring describes."""

    def test_raw_columns_are_exactly_the_expected_four(self):
        self.assertEqual(_RAW_COLUMNS, ("_snapshot_id", "_source_row_number", "_loaded_at", "raw_row"))


class TestRelationComposition(unittest.TestCase):
    def test_valid_schema_and_table_compose(self):
        self.assertEqual(_relation("raw", "eligibility"), '"raw"."eligibility"')

    def test_hostile_schema_rejected(self):
        with self.assertRaises(InvalidIdentifierError):
            _relation("raw; DROP TABLE x", "eligibility")

    def test_every_raw_table_name_is_itself_a_valid_identifier(self):
        for table in RAW_TABLES:
            # Must not raise.
            _relation("raw", table)


class TestVerifyFileChecksum(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "eligibility.csv"
        self.path.write_text("patient_id\n1\n", encoding="utf-8")
        self.correct_sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_matching_checksum_does_not_raise(self):
        verify_file_checksum(self.path, self.correct_sha256, table="eligibility")

    def test_mismatched_checksum_raises(self):
        with self.assertRaises(RawLoadError) as ctx:
            verify_file_checksum(self.path, "f" * 64, table="eligibility")
        self.assertIn("eligibility", str(ctx.exception))

    def test_corruption_after_extraction_is_detected(self):
        original = self.path.read_text(encoding="utf-8")
        self.path.write_text(original + "tampered\n", encoding="utf-8")
        with self.assertRaises(RawLoadError):
            verify_file_checksum(self.path, self.correct_sha256, table="eligibility")


class TestIterCsvRows(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_rows_yielded_as_header_keyed_mappings(self):
        path = self.dir / "eligibility.csv"
        path.write_text("patient_id,payer\n1,Acme\n2,Beta\n", encoding="utf-8")
        rows = list(_iter_csv_rows(path))
        self.assertEqual(rows, [{"patient_id": "1", "payer": "Acme"}, {"patient_id": "2", "payer": "Beta"}])

    def test_quoted_fields_with_embedded_commas_handled(self):
        path = self.dir / "medical_claim.csv"
        path.write_text('claim_id,payer\n1,"Acme, Inc."\n', encoding="utf-8")
        rows = list(_iter_csv_rows(path))
        self.assertEqual(rows[0]["payer"], "Acme, Inc.")

    def test_no_header_row_raises(self):
        path = self.dir / "empty.csv"
        path.write_text("", encoding="utf-8")
        with self.assertRaises(RawLoadError):
            list(_iter_csv_rows(path))

    def test_header_only_yields_no_rows(self):
        path = self.dir / "header_only.csv"
        path.write_text("patient_id,payer\n", encoding="utf-8")
        self.assertEqual(list(_iter_csv_rows(path)), [])


if __name__ == "__main__":
    unittest.main()
