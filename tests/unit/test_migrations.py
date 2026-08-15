"""Standard-library unit tests for the migration-discovery/checksum logic
in tuva_postgres.migrations that don't require a real PostgreSQL
connection (see tests/integration/test_pipeline_integration.py for the
disposable-database coverage of apply_pending/status).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tuva_postgres.db import substitute_psql_vars  # noqa: E402
from tuva_postgres.errors import MigrationError  # noqa: E402
from tuva_postgres.migrations import compute_checksum, discover  # noqa: E402

# Pinned pre-refactor checksums for the real, committed migrations 0001 and
# 0002 -- captured with this same compute_checksum implementation against
# db/tables/*.sql / db/migrations/sql/0002_operational_schema.sql before
# those files were reorganized into version-owned directories under
# db/migrations/sql/. These must never change: a database that already
# recorded these migrations as applied must never see a checksum mismatch.
REAL_0001_CHECKSUM = "6a7cfe125ac4becc4e18000ced22530394ed0d5bda0b7898928820bce83a0445"
REAL_0002_CHECKSUM = "fd8a57293ec70b8f78a9347ec2eba571d5b375100cd665c733dc2d536397a2e4"


class TestSubstitutePsqlVars(unittest.TestCase):
    def test_identifier_form(self):
        out = substitute_psql_vars('ALTER TABLE :"schema".patient ...', {"schema": "tuva"})
        self.assertEqual(out, 'ALTER TABLE "tuva".patient ...')

    def test_string_literal_form(self):
        out = substitute_psql_vars("WHERE n.nspname = :'schema'", {"schema": "tuva"})
        self.assertEqual(out, "WHERE n.nspname = 'tuva'")

    def test_embedded_quotes_are_escaped(self):
        out = substitute_psql_vars(':"schema"', {"schema": 'weird"name'})
        self.assertEqual(out, '"weird""name"')

    def test_does_not_touch_type_casts(self):
        # `::date` etc. must never be mistaken for a psql variable.
        out = substitute_psql_vars("value::date", {"schema": "tuva"})
        self.assertEqual(out, "value::date")


class TestDiscoverAndChecksum(unittest.TestCase):
    """Synthetic fixtures use the same layout convention enforced by
    discover() and used by the real repository: every migration's SQL
    lives under db/migrations/sql/<manifest-filename-stem>/."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)

    def _write_sql(self, subdir: str, name: str, content: str) -> Path:
        d = self.migrations_dir / "sql" / subdir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text(content, encoding="utf-8")
        return path

    def _write_manifest(self, filename: str, version: str, files: list[str], description="test migration"):
        (self.migrations_dir / filename).write_text(
            json.dumps({"version": version, "description": description, "files": files, "vars": {}}) + "\n",
            encoding="utf-8",
        )

    def test_real_repo_migrations_discover_without_error(self):
        # Exercises the actual, committed db/migrations/*.json against the
        # actual repo -- not a synthetic fixture -- so a broken manifest
        # (missing file, bad version, duplicate, out-of-layout reference)
        # fails this test directly.
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        migrations = discover(real_migrations_dir, REPO_ROOT)
        self.assertGreaterEqual(len(migrations), 2)
        self.assertEqual([m.version for m in migrations], sorted(m.version for m in migrations))
        for migration in migrations:
            checksum = compute_checksum(migration)
            self.assertEqual(len(checksum), 64)

    def test_real_repo_migration_0001_checksum_matches_pre_refactor_value(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        by_version = {m.version: m for m in discover(real_migrations_dir, REPO_ROOT)}
        self.assertIn("0001", by_version)
        self.assertEqual(compute_checksum(by_version["0001"]), REAL_0001_CHECKSUM)

    def test_real_repo_migration_0002_checksum_matches_pre_refactor_value(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        by_version = {m.version: m for m in discover(real_migrations_dir, REPO_ROOT)}
        self.assertIn("0002", by_version)
        self.assertEqual(compute_checksum(by_version["0002"]), REAL_0002_CHECKSUM)

    def test_real_repo_no_manifest_references_db_tables(self):
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        for manifest_path in sorted(real_migrations_dir.glob("*.json")):
            raw = manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("db/tables", raw, f"{manifest_path.name} still references db/tables/")

    def test_deterministic_ordering(self):
        self._write_sql("0002_b", "b.sql", "SELECT 1;")
        self._write_manifest("0002_b.json", "0002", ["db/migrations/sql/0002_b/b.sql"])
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])

        migrations = discover(self.migrations_dir, self.repo_root)
        self.assertEqual([m.version for m in migrations], ["0001", "0002"])

    def test_duplicate_version_rejected(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        self._write_sql("0001_b", "b.sql", "SELECT 1;")
        self._write_manifest("0001_b.json", "0001", ["db/migrations/sql/0001_b/b.sql"])

        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_missing_referenced_file_rejected(self):
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/does_not_exist.sql"])
        with self.assertRaises(MigrationError):
            discover(self.migrations_dir, self.repo_root)

    def test_path_traversal_rejected(self):
        # A manifest must never be able to reference a file outside the
        # repository, regardless of how many "../" segments it uses.
        self._write_manifest("0001_a.json", "0001", ["../outside.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("outside the repository", str(ctx.exception).lower())

    def test_file_outside_migrations_sql_dir_rejected(self):
        # Deployable migration SQL must live under db/migrations/sql/, not
        # some other, mutable directory (like the retired db/tables/).
        other_dir = self.repo_root / "other"
        other_dir.mkdir()
        (other_dir / "file.sql").write_text("SELECT 1;", encoding="utf-8")
        self._write_manifest("0001_a.json", "0001", ["other/file.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("db/migrations/sql", str(ctx.exception))

    def test_file_outside_owning_migration_directory_rejected(self):
        # A migration must not be able to reference another migration's
        # version-owned directory.
        self._write_sql("0002_b", "b.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0002_b/b.sql"])
        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("own", str(ctx.exception).lower())

    def test_checksum_changes_when_file_content_changes(self):
        path = self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        migrations = discover(self.migrations_dir, self.repo_root)
        checksum_before = compute_checksum(migrations[0])

        path.write_text("SELECT 2;", encoding="utf-8")
        migrations_after = discover(self.migrations_dir, self.repo_root)
        checksum_after = compute_checksum(migrations_after[0])

        self.assertNotEqual(checksum_before, checksum_after)

    def test_checksum_stable_for_unchanged_files(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        m1 = discover(self.migrations_dir, self.repo_root)[0]
        m2 = discover(self.migrations_dir, self.repo_root)[0]
        self.assertEqual(compute_checksum(m1), compute_checksum(m2))

    def test_reordering_files_changes_checksum(self):
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_sql("0001_a", "b.sql", "SELECT 2;")
        self._write_manifest(
            "0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql", "db/migrations/sql/0001_a/b.sql"]
        )
        checksum_ab = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        self._write_manifest(
            "0001_a.json", "0001", ["db/migrations/sql/0001_a/b.sql", "db/migrations/sql/0001_a/a.sql"]
        )
        checksum_ba = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        self.assertNotEqual(checksum_ab, checksum_ba)

    def test_moving_unchanged_file_preserves_checksum(self):
        # Moving a file without changing its basename or bytes must not
        # change its migration's checksum -- this is exactly how the real
        # 0001/0002 migrations were reorganized into version-owned
        # directories without invalidating already-applied databases.
        self._write_sql("0001_a", "a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["db/migrations/sql/0001_a/a.sql"])
        checksum_before = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])

        new_dir = self.migrations_dir / "sql" / "0001_renamed"
        new_dir.mkdir(parents=True)
        (self.migrations_dir / "sql" / "0001_a" / "a.sql").rename(new_dir / "a.sql")
        (self.migrations_dir / "0001_a.json").unlink()
        self._write_manifest("0001_renamed.json", "0001", ["db/migrations/sql/0001_renamed/a.sql"])

        checksum_after = compute_checksum(discover(self.migrations_dir, self.repo_root)[0])
        self.assertEqual(checksum_before, checksum_after)


if __name__ == "__main__":
    unittest.main()
