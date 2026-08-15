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
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo_root = Path(self._tmp.name)
        self.migrations_dir = self.repo_root / "db" / "migrations"
        self.migrations_dir.mkdir(parents=True)
        (self.repo_root / "sql").mkdir()

    def _write_sql(self, name: str, content: str) -> Path:
        path = self.repo_root / "sql" / name
        path.write_text(content, encoding="utf-8")
        return path

    def _write_manifest(self, name: str, version: str, files: list[str], description="test migration"):
        (self.migrations_dir / name).write_text(
            json.dumps({"version": version, "description": description, "files": files, "vars": {}}) + "\n",
            encoding="utf-8",
        )

    def test_real_repo_migrations_discover_without_error(self):
        # Exercises the actual, committed db/migrations/*.json against the
        # actual repo -- not a synthetic fixture -- so a broken manifest
        # (missing file, bad version, duplicate) fails this test directly.
        real_migrations_dir = REPO_ROOT / "db" / "migrations"
        migrations = discover(real_migrations_dir, REPO_ROOT)
        self.assertGreaterEqual(len(migrations), 2)
        self.assertEqual([m.version for m in migrations], sorted(m.version for m in migrations))
        for migration in migrations:
            checksum = compute_checksum(migration)
            self.assertEqual(len(checksum), 64)

    def test_deterministic_ordering(self):
        self._write_sql("b.sql", "SELECT 1;")
        self._write_manifest("0002_b.json", "0002", ["sql/b.sql"])
        self._write_sql("a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["sql/a.sql"])

        migrations = discover(self.migrations_dir, self.repo_root)
        self.assertEqual([m.version for m in migrations], ["0001", "0002"])

    def test_duplicate_version_rejected(self):
        self._write_sql("a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["sql/a.sql"])
        self._write_sql("b.sql", "SELECT 1;")
        self._write_manifest("0001_b.json", "0001", ["sql/b.sql"])

        with self.assertRaises(MigrationError) as ctx:
            discover(self.migrations_dir, self.repo_root)
        self.assertIn("duplicate", str(ctx.exception).lower())

    def test_missing_referenced_file_rejected(self):
        self._write_manifest("0001_a.json", "0001", ["sql/does_not_exist.sql"])
        with self.assertRaises(MigrationError):
            discover(self.migrations_dir, self.repo_root)

    def test_checksum_changes_when_file_content_changes(self):
        path = self._write_sql("a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["sql/a.sql"])
        migrations = discover(self.migrations_dir, self.repo_root)
        checksum_before = compute_checksum(migrations[0])

        path.write_text("SELECT 2;", encoding="utf-8")
        migrations_after = discover(self.migrations_dir, self.repo_root)
        checksum_after = compute_checksum(migrations_after[0])

        self.assertNotEqual(checksum_before, checksum_after)

    def test_checksum_stable_for_unchanged_files(self):
        self._write_sql("a.sql", "SELECT 1;")
        self._write_manifest("0001_a.json", "0001", ["sql/a.sql"])
        m1 = discover(self.migrations_dir, self.repo_root)[0]
        m2 = discover(self.migrations_dir, self.repo_root)[0]
        self.assertEqual(compute_checksum(m1), compute_checksum(m2))


if __name__ == "__main__":
    unittest.main()
