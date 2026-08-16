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
from dataclasses import dataclass
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
    load_single_endpoint_snapshot,
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




class _ExplodingConnection:
    """A fake psycopg connection that raises if it is ever touched --
    used to prove load_single_endpoint_snapshot's error paths (missing
    file, missing checksum, checksum mismatch) fail before any database
    interaction, never TRUNCATEing or COPYing anything."""

    def cursor(self):
        raise AssertionError("the connection must not be touched when validation fails before load_table")


@dataclass
class _FakeConfigForLoader:
    raw_schema: str = "raw"


class TestLoadSingleEndpointSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.snapshot_dir = Path(self._tmp.name)
        self.config = _FakeConfigForLoader()

    def test_missing_csv_raises_before_touching_connection(self):
        with self.assertRaises(RawLoadError) as ctx:
            load_single_endpoint_snapshot(
                _ExplodingConnection(), self.config, self.snapshot_dir, "snap-1", "eligibility", {}
            )
        self.assertIn("eligibility", str(ctx.exception))

    def test_missing_checksum_raises_before_touching_connection(self):
        (self.snapshot_dir / "eligibility.csv").write_text("patient_id\n1\n", encoding="utf-8")
        with self.assertRaises(RawLoadError) as ctx:
            load_single_endpoint_snapshot(
                _ExplodingConnection(), self.config, self.snapshot_dir, "snap-1", "eligibility", {}
            )
        self.assertIn("no recorded checksum", str(ctx.exception))

    def test_checksum_mismatch_raises_before_touching_connection(self):
        (self.snapshot_dir / "eligibility.csv").write_text("patient_id\n1\n", encoding="utf-8")
        checksums = {"eligibility": {"sha256": "f" * 64, "size_bytes": 5}}
        with self.assertRaises(RawLoadError):
            load_single_endpoint_snapshot(
                _ExplodingConnection(), self.config, self.snapshot_dir, "snap-1", "eligibility", checksums
            )

    def test_never_references_tables_other_than_the_requested_one(self):
        # The function signature takes a single `table` argument and
        # only ever builds a relation from it (see raw_loader.py) -- this
        # regression test locks in that `RAW_TABLES` (the full 3-table
        # set) is never consulted by this function at all.
        import inspect

        source = inspect.getsource(load_single_endpoint_snapshot)
        self.assertNotIn("RAW_TABLES", source)


if __name__ == "__main__":
    unittest.main()
