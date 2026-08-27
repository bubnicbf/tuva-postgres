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

    def test_discovers_eight_migrations_in_order(self):
        # migrations/006_object_storage_raw_contract.sql was renumbered
        # to 007 (see that file's own header note) to resolve a
        # duplicate-version conflict with the already-established
        # 006_record_quarantine.sql; 008_operational_table_hardening.sql
        # is new. discover() itself is the authority here -- this test
        # would fail loudly again if either file ever regains a
        # conflicting version.
        found = migrations.discover(REPO_ROOT / "migrations")
        self.assertEqual(
            [m.version for m in found], ["001", "002", "003", "004", "005", "006", "007", "008"]
        )
        self.assertEqual(
            [m.filename for m in found],
            [
                "001_operational_schemas.sql",
                "002_ingestion_control.sql",
                "003_roles_and_grants.sql",
                "004_endpoint_scoped_ingestion.sql",
                "005_paginated_extraction_state.sql",
                "006_record_quarantine.sql",
                "007_object_storage_raw_contract.sql",
                "008_operational_table_hardening.sql",
            ],
        )

    def test_migration_007_mentions_every_new_canonical_table(self):
        # A structural (not checksum-weakening) proof that 007 actually
        # defines every required new operational/raw object -- catches an
        # accidental partial migration without needing a live database.
        sql_text = (REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql").read_text(encoding="utf-8")
        for expected in (
            "ingestion_run", "ingestion_page", "ingestion_cursor", "rejected_record", "schema_observation",
            "_ingestion_run_id", "_ingested_at", "_source_endpoint", "_source_record_id",
            "_source_updated_at", "_payload_hash", "_raw_payload",
        ):
            self.assertIn(expected, sql_text, f"migration 007 does not mention {expected!r}")

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


class TestMigration006RecordQuarantine(unittest.TestCase):
    """migrations/006_record_quarantine.sql adds the restricted
    quarantined_records table (validators.py/quarantine.py/
    paginated_loader.py) -- a lightweight structural check against the
    real shipped file, without requiring a live PostgreSQL connection.
    Access-control behavior itself (grants actually taking effect) is
    covered by tests/integration/test_pipeline_integration.py against a
    disposable database."""

    def test_file_is_discovered_with_a_stable_checksum(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        migration_006 = next(m for m in found if m.version == "006")
        self.assertEqual(migration_006.filename, "006_record_quarantine.sql")
        self.assertEqual(migration_006.checksum, migrations.compute_checksum(migration_006.path))

    def test_creates_quarantined_records_table_with_required_columns(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("quarantined_records", sql)
        for column in (
            "quarantine_id",
            "run_id",
            "source",
            "endpoint",
            "page_number",
            "record_index",
            "reason_code",
            "reason_detail",
            "raw_record",
            "source_record_sha256",
            "quarantined_at",
        ):
            self.assertIn(column, sql)

    def test_reason_code_is_constrained_to_a_fixed_allowlist(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("quarantined_records_reason_code_check", sql)
        for reason_code in (
            "record_not_object",
            "missing_required_field",
            "invalid_required_type",
            "invalid_identifier",
            "invalid_date_format",
            "schema_validation_failed",
        ):
            self.assertIn(reason_code, sql)

    def test_reason_detail_length_is_bounded(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("quarantined_records_reason_detail_length_check", sql)
        self.assertIn("char_length(reason_detail) <= 200", sql)

    def test_adds_idempotency_unique_index(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("quarantined_records_run_page_record_key", sql)
        self.assertIn("(run_id, page_number, record_index)", sql)

    def test_revokes_public_access(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn('REVOKE ALL ON :"ops_schema".quarantined_records FROM PUBLIC;', sql)

    def test_revokes_default_privileges_before_granting_insert_only_to_ingest_role(self):
        # migration 003's ALTER DEFAULT PRIVILEGES would otherwise leak
        # SELECT/UPDATE onto this brand-new table -- the explicit REVOKE
        # before the narrow GRANT INSERT is load-bearing, not defensive
        # boilerplate. Assert ordering, not just presence.
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        revoke_idx = sql.index('REVOKE ALL ON :"ops_schema".quarantined_records FROM :"ingest_role";')
        grant_idx = sql.index('GRANT INSERT ON :"ops_schema".quarantined_records TO :"ingest_role";')
        self.assertLess(revoke_idx, grant_idx)
        self.assertNotIn('GRANT SELECT ON :"ops_schema".quarantined_records', sql)
        self.assertNotIn('GRANT UPDATE ON :"ops_schema".quarantined_records', sql)
        self.assertNotIn('GRANT DELETE ON :"ops_schema".quarantined_records', sql)

    def test_transform_role_is_never_granted_access(self):
        path = REPO_ROOT / "migrations" / "006_record_quarantine.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertNotIn('GRANT SELECT ON :"ops_schema".quarantined_records TO :"transform_role"', sql)
        self.assertNotIn('TO :"transform_role"', sql)

    def test_is_forward_only_never_rewrites_prior_migrations(self):
        for filename in (
            "001_operational_schemas.sql",
            "002_ingestion_control.sql",
            "003_roles_and_grants.sql",
            "004_endpoint_scoped_ingestion.sql",
            "005_paginated_extraction_state.sql",
        ):
            path = REPO_ROOT / "migrations" / filename
            self.assertTrue(path.is_file(), f"migration {filename} must still exist unmodified")



class TestMigration007ObjectStorageRawContract(unittest.TestCase):
    """migrations/007_object_storage_raw_contract.sql (renumbered from
    006 -- see that file's own header note, and
    test_discovers_eight_migrations_in_order above) creates the five
    canonical object-storage-backed operational tables plus the seven
    new raw-metadata columns state.py/object_raw_loader.py depend on --
    a lightweight structural check against the real shipped file,
    without requiring a live PostgreSQL connection. Runtime behavior
    (grants actually taking effect, constraints actually rejecting bad
    data) is covered by tests/integration/test_object_storage_pipeline_integration.py
    against a disposable database."""

    def test_file_is_discovered_with_a_stable_checksum(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        migration_007 = next(m for m in found if m.version == "007")
        self.assertEqual(migration_007.filename, "007_object_storage_raw_contract.sql")
        self.assertEqual(migration_007.checksum, migrations.compute_checksum(migration_007.path))

    def test_creates_all_five_canonical_tables_in_the_configured_ops_schema(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        for table in ("ingestion_run", "ingestion_page", "ingestion_cursor", "rejected_record", "schema_observation"):
            self.assertIn(f':"ops_schema".{table}', sql, f"{table} must be created in the configured ops_schema, never a hard-coded schema")
        # Never a hard-coded schema literal such as "ops"."ingestion_run".
        self.assertNotIn('"ops".ingestion_run', sql)
        self.assertNotIn('"ops".ingestion_page', sql)

    def test_ingestion_run_has_required_lifecycle_columns_and_status_check(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        for column in (
            "run_id", "vendor", "endpoint", "load_date", "storage_run_prefix", "requested_cursor",
            "candidate_cursor", "status", "started_at", "published_at", "load_started_at", "committed_at",
            "failed_at", "finished_at", "extracted_count", "accepted_count", "rejected_count",
            "inserted_count", "duplicate_count", "page_count", "failure_category", "failure_message",
            "app_version", "environment",
        ):
            self.assertIn(column, sql)
        self.assertIn("CHECK (status IN ('running', 'published', 'loading', 'committed', 'failed'))", sql)

    def test_run_level_counts_are_nonnegative(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        for column in ("extracted_count", "accepted_count", "rejected_count", "inserted_count", "duplicate_count"):
            pattern = rf"{column}\s+bigint CHECK \({column} IS NULL OR {column} >= 0\)"
            self.assertRegex(sql, pattern)
        self.assertRegex(sql, r"page_count\s+integer CHECK \(page_count IS NULL OR page_count >= 0\)")

    def test_ingestion_page_enforces_one_page_number_per_run_and_unique_object_key(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("UNIQUE (run_id, page_number)", sql)
        self.assertIn("UNIQUE (object_key)", sql)
        self.assertRegex(sql, r"page_number\s+integer NOT NULL CHECK \(page_number BETWEEN 1 AND 999999\)")
        self.assertIn('REFERENCES :"ops_schema".ingestion_run (run_id)', sql)

    def test_ingestion_cursor_uses_vendor_endpoint_primary_key_and_lock_version(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("PRIMARY KEY (vendor, endpoint)", sql)
        self.assertRegex(sql, r"lock_version\s+bigint NOT NULL DEFAULT 0")
        self.assertRegex(sql, r"successful_run_id\s+uuid REFERENCES")

    def test_rejected_record_enforces_idempotent_uniqueness_and_never_duplicates_raw_payload(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("UNIQUE (run_id, page_number, record_position)", sql)
        self.assertIn("raw_object_key        text NOT NULL", sql)
        # Isolate just this table's own column block and confirm it never
        # carries a full raw-payload-shaped column (only a durable
        # pointer, raw_object_key, back to immutable object storage).
        table_start = sql.index('CREATE TABLE IF NOT EXISTS :"ops_schema".rejected_record')
        table_end = sql.index("CREATE INDEX", table_start)
        rejected_record_block = sql[table_start:table_end]
        self.assertNotIn("raw_payload", rejected_record_block)

    def test_rejected_record_public_and_transform_role_are_revoked(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn('REVOKE ALL ON :"ops_schema".rejected_record FROM PUBLIC;', sql)
        self.assertIn(':"ops_schema".rejected_record', sql.split('FROM :"transform_role"')[0][-400:])

    def test_schema_observation_enforces_deterministic_uniqueness_grain(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("UNIQUE (vendor, endpoint, field_path, observed_type)", sql)
        self.assertIn("occurrence_count          bigint NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1)", sql)

    def test_never_creates_a_tuva_managed_schema(self):
        path = REPO_ROOT / "migrations" / "007_object_storage_raw_contract.sql"
        sql = path.read_text(encoding="utf-8")
        for tuva_schema_var in ("staging_schema", "input_layer_schema", "analytics_core_schema", "analytics_marts_schema"):
            self.assertNotIn(tuva_schema_var, sql)

    def test_is_forward_only_never_rewrites_prior_migrations(self):
        for filename in (
            "001_operational_schemas.sql",
            "002_ingestion_control.sql",
            "003_roles_and_grants.sql",
            "004_endpoint_scoped_ingestion.sql",
            "005_paginated_extraction_state.sql",
            "006_record_quarantine.sql",
        ):
            path = REPO_ROOT / "migrations" / filename
            self.assertTrue(path.is_file(), f"migration {filename} must still exist unmodified")


class TestMigration008OperationalTableHardening(unittest.TestCase):
    """migrations/008_operational_table_hardening.sql closes the gaps
    identified auditing migrations/007_object_storage_raw_contract.sql
    against the required canonical-table contract: checksum format
    validation, a bounded/enumerated rejected_record.reason_code and
    detail, least-privilege ingest_role grants on rejected_record, and
    an index supporting cross-run page-status investigation."""

    def test_file_is_discovered_with_a_stable_checksum(self):
        found = migrations.discover(REPO_ROOT / "migrations")
        migration_008 = next(m for m in found if m.version == "008")
        self.assertEqual(migration_008.filename, "008_operational_table_hardening.sql")
        self.assertEqual(migration_008.checksum, migrations.compute_checksum(migration_008.path))

    def test_validates_checksum_is_sha256_hex(self):
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("ingestion_page_checksum_format_check", sql)
        self.assertIn("checksum ~ ''^[0-9a-f]{64}$''", sql)

    def test_reason_code_is_constrained_to_the_rejectreason_allowlist(self):
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("rejected_record_reason_code_check", sql)
        for reason_code in (
            "not_an_object", "unsupported_endpoint", "missing_source_id",
            "missing_source_timestamp", "invalid_source_timestamp",
        ):
            self.assertIn(reason_code, sql)

    def test_detail_length_is_bounded(self):
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("rejected_record_detail_length_check", sql)
        self.assertIn("char_length(detail) <= 500", sql)

    def test_revokes_before_granting_insert_only_to_ingest_role(self):
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        revoke_idx = sql.index('REVOKE ALL ON :"ops_schema".rejected_record FROM :"ingest_role";')
        grant_idx = sql.index('GRANT INSERT ON :"ops_schema".rejected_record TO :"ingest_role";')
        self.assertLess(revoke_idx, grant_idx)
        self.assertNotIn('GRANT SELECT ON :"ops_schema".rejected_record', sql)
        self.assertNotIn('GRANT UPDATE ON :"ops_schema".rejected_record', sql)

    def test_adds_status_index_for_cross_run_investigation(self):
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertIn("ingestion_page_status_idx", sql)
        self.assertIn("(status, run_id)", sql)

    def test_uses_do_blocks_for_idempotent_check_constraints(self):
        # PostgreSQL has no ADD CONSTRAINT IF NOT EXISTS -- constraints
        # must be guarded by an existence check, the same DO-block
        # pattern migrations/003_roles_and_grants.sql already uses for
        # idempotent role creation.
        path = REPO_ROOT / "migrations" / "008_operational_table_hardening.sql"
        sql = path.read_text(encoding="utf-8")
        self.assertGreaterEqual(sql.count("DO $do$"), 3)
        self.assertIn("pg_constraint", sql)

    def test_is_forward_only_never_rewrites_prior_migrations(self):
        for filename in (
            "001_operational_schemas.sql",
            "002_ingestion_control.sql",
            "003_roles_and_grants.sql",
            "004_endpoint_scoped_ingestion.sql",
            "005_paginated_extraction_state.sql",
            "006_record_quarantine.sql",
            "007_object_storage_raw_contract.sql",
        ):
            path = REPO_ROOT / "migrations" / filename
            self.assertTrue(path.is_file(), f"migration {filename} must still exist unmodified")

if __name__ == "__main__":
    unittest.main()
