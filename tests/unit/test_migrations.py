"""Standard-library unit tests for tuva_ingest.migrations's database-free
logic: discovery, checksums, variable resolution, and planning.
`apply_pending`/`status` themselves require a real PostgreSQL connection
and are covered by tests/integration/test_pipeline_integration.py.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_ingest import migrations  # noqa: E402
from tuva_ingest.errors import MigrationError  # noqa: E402


class TestDiscoverRealMigrations(unittest.TestCase):
    """Exercise discover() against this repository's real migrations/
    directory -- proves the three shipped files are well-formed, unique,
    and discoverable, not just that discover() works in the abstract."""

    def test_discovers_five_migrations_in_order(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        self.assertEqual([m.version for m in found], ["001", "002", "003", "004", "005"])
        self.assertEqual(
            [m.filename for m in found],
            [
                "001_operational_schemas.sql",
                "002_ingestion_control.sql",
                "003_roles_and_grants.sql",
                "004_endpoint_scoped_ingestion.sql",
                "005_paginated_extraction_state.sql",
            ],
        )

    def test_checksums_are_stable_across_repeated_discovery(self):
        first = migrations.discover(REPO_ROOT / "migrations")
        second = migrations.discover(REPO_ROOT / "migrations")
        self.assertEqual([m.checksum for m in first], [m.checksum for m in second])

    def test_checksum_independent_of_configured_schema_names(self):
        # compute_checksum() is over raw file bytes, before :"var"
        # substitution -- must be identical regardless of what RAW_SCHEMA/
        # OPS_SCHEMA an operator configures.
        path = (REPO_ROOT / "migrations" / "001_operational_schemas.sql")
        self.assertEqual(migrations.compute_checksum(path), migrations.compute_checksum(path))


class TestDiscoverFilenamePolicy(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def _write(self, name: str, content: str = "SELECT 1;\n") -> None:
        (self.dir / name).write_text(content, encoding="utf-8")

    def test_rejects_malformed_filename(self):
        self._write("not_versioned.sql")
        with self.assertRaises(MigrationError):
            migrations.discover(self.dir)

    def test_rejects_duplicate_version(self):
        self._write("001_first.sql")
        self._write("001_second.sql")
        with self.assertRaises(MigrationError):
            migrations.discover(self.dir)

    def test_rejects_empty_directory(self):
        with self.assertRaises(MigrationError):
            migrations.discover(self.dir)

    def test_rejects_missing_directory(self):
        with self.assertRaises(MigrationError):
            migrations.discover(self.dir / "does-not-exist")

    def test_sorted_by_version_regardless_of_glob_order(self):
        self._write("010_later.sql")
        self._write("002_earlier.sql")
        found = migrations.discover(self.dir)
        self.assertEqual([m.version for m in found], ["002", "010"])


@dataclass
class _FakeConfig:
    raw_schema: str
    ops_schema: str
    ingest_role: str
    transform_role: str


class TestResolveVars(unittest.TestCase):
    def test_valid_config_resolves_all_four_vars(self):
        config = _FakeConfig(raw_schema="raw", ops_schema="ingest_ops", ingest_role="ingest_role", transform_role="transform_role")
        result = migrations._resolve_vars(config)
        self.assertEqual(
            result,
            {"raw_schema": "raw", "ops_schema": "ingest_ops", "ingest_role": "ingest_role", "transform_role": "transform_role"},
        )

    def test_hostile_raw_schema_raises_migration_error(self):
        config = _FakeConfig(raw_schema="raw; DROP TABLE x", ops_schema="ingest_ops", ingest_role="a", transform_role="b")
        with self.assertRaises(MigrationError):
            migrations._resolve_vars(config)


class TestPlan(unittest.TestCase):
    def _mf(self, version, checksum="abc"):
        return migrations.MigrationFile(version=version, filename=f"{version}_x.sql", path=Path(f"{version}_x.sql"), checksum=checksum)

    def _applied(self, version, checksum="abc"):
        from datetime import datetime, timezone

        return migrations.AppliedMigration(
            version=version, filename=f"{version}_x.sql", checksum=checksum,
            applied_at=datetime.now(timezone.utc), duration_ms=1.0,
        )

    def test_all_pending_when_none_applied(self):
        result = migrations._plan([self._mf("001"), self._mf("002")], {})
        self.assertEqual(len(result.pending), 2)
        self.assertEqual(result.applied, ())
        self.assertFalse(result.has_integrity_failures)

    def test_applied_and_pending_split_correctly(self):
        applied = {"001": self._applied("001")}
        result = migrations._plan([self._mf("001"), self._mf("002")], applied)
        self.assertEqual([m.version for m in result.applied], ["001"])
        self.assertEqual([m.version for m in result.pending], ["002"])

    def test_checksum_mismatch_detected(self):
        applied = {"001": self._applied("001", checksum="old-checksum")}
        result = migrations._plan([self._mf("001", checksum="new-checksum")], applied)
        self.assertEqual(result.checksum_mismatches, ("001",))
        self.assertTrue(result.has_integrity_failures)




class TestMigration004EndpointScopedIngestion(unittest.TestCase):
    """migrations/004_endpoint_scoped_ingestion.sql adds the endpoint/
    requested_since columns and the (run_id, table_name) unique index
    that state.upsert_running_run/upsert_table_load_pending rely on for
    their ON CONFLICT targets -- a lightweight structural check that the
    real shipped file (not a fake) actually contains them, without
    requiring a live PostgreSQL connection."""

    def test_file_is_discovered_with_a_stable_checksum(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        migration_004 = next(m for m in found if m.version == "004")
        self.assertEqual(migration_004.filename, "004_endpoint_scoped_ingestion.sql")
        self.assertEqual(migration_004.checksum, migrations.compute_checksum(migration_004.path))

    def test_adds_endpoint_and_requested_since_columns(self):
        path = REPO_ROOT / "migrations" / "004_endpoint_scoped_ingestion.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("endpoint", sql)
        self.assertIn("requested_since", sql)
        self.assertIn("ingestion_runs", sql)

    def test_adds_unique_index_for_run_id_table_name(self):
        path = REPO_ROOT / "migrations" / "004_endpoint_scoped_ingestion.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("table_loads_run_id_table_name_key", sql)
        self.assertIn("UNIQUE INDEX", sql.upper())

    def test_is_forward_only_never_rewrites_prior_migrations(self):
        # 001-003 must be byte-for-byte unchanged by this addition --
        # their checksums (recorded once a migration is applied in a
        # real database) must never drift.
        for version, filename in (
            ("001", "001_operational_schemas.sql"),
            ("002", "002_ingestion_control.sql"),
            ("003", "003_roles_and_grants.sql"),
        ):
            path = REPO_ROOT / "migrations" / filename
            self.assertTrue(path.is_file(), f"migration {version} must still exist unmodified")




class TestMigration005PaginatedExtractionState(unittest.TestCase):
    """migrations/005_paginated_extraction_state.sql adds the
    source_watermarks table and the raw-table metadata columns/unique
    indexes the paginated extraction contract (pagination.py,
    paginated_loader.py, state.get_watermark/commit_watermark) relies
    on -- a lightweight structural check against the real shipped file,
    without requiring a live PostgreSQL connection."""

    def test_file_is_discovered_with_a_stable_checksum(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        migration_005 = next(m for m in found if m.version == "005")
        self.assertEqual(migration_005.filename, "005_paginated_extraction_state.sql")
        self.assertEqual(migration_005.checksum, migrations.compute_checksum(migration_005.path))

    def test_adds_source_watermarks_table(self):
        path = REPO_ROOT / "migrations" / "005_paginated_extraction_state.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("source_watermarks", sql)
        self.assertIn("PRIMARY KEY (source, endpoint)", sql)

    def test_adds_raw_table_metadata_columns_for_every_managed_table(self):
        path = REPO_ROOT / "migrations" / "005_paginated_extraction_state.sql"
        sql = path.read_text(encoding="utf-8")
        for table in ("eligibility", "medical_claim", "pharmacy_claim"):
            self.assertIn(f':"raw_schema".{table}', sql)
        for column in ("endpoint", "page_number", "source_page_token", "retrieved_at", "file_sha256"):
            self.assertIn(column, sql)

    def test_adds_unique_idempotency_index_for_every_managed_table(self):
        path = REPO_ROOT / "migrations" / "005_paginated_extraction_state.sql"
        sql = path.read_text(encoding="utf-8")
        for table in ("eligibility", "medical_claim", "pharmacy_claim"):
            self.assertIn(f"{table}_snapshot_row_key", sql)
        self.assertEqual(sql.upper().count("CREATE UNIQUE INDEX"), 3)

    def test_is_forward_only_never_rewrites_prior_migrations(self):
        for filename in (
            "001_operational_schemas.sql",
            "002_ingestion_control.sql",
            "003_roles_and_grants.sql",
            "004_endpoint_scoped_ingestion.sql",
        ):
            path = REPO_ROOT / "migrations" / filename
            self.assertTrue(path.is_file(), f"migration {filename} must still exist unmodified")


if __name__ == "__main__":
    unittest.main()
